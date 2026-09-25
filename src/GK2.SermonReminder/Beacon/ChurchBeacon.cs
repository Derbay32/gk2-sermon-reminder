using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using LazyBearTechnology;
using UnityEngine;
using UnityEngine.UI;

namespace GK2.SermonReminder.Beacon
{
    /// <summary>
    /// Owned beacon availability. <see cref="Inactive"/> is the normal not-eligible
    /// state (gates shut, off-sermon-day, opportunity consumed, switch off, state
    /// unreadable); <see cref="Pending"/> is ordinary unreadiness while the native
    /// HUD is not live; <see cref="Healthy"/> is a drawn beacon; <see cref="Failed"/>
    /// is a real resource/binding fault that is retried on a bounded backoff.
    /// </summary>
    internal enum BeaconStatus
    {
        /// <summary>Not eligible, or explicitly reset: nothing owned is shown.</summary>
        Inactive,

        /// <summary>Eligible but the native HUD is not live right now; ordinary, never a fault.</summary>
        Pending,

        /// <summary>Eligible and the owned beacon was drawn this tick.</summary>
        Healthy,

        /// <summary>A resource/binding fault; the owned beacon is cleared and retried later.</summary>
        Failed
    }

    /// <summary>
    /// Which dependency failed, so the owner can own diagnostics and recovery
    /// (this core never logs and never invents an alternative source).
    /// </summary>
    internal enum BeaconFailureStage
    {
        /// <summary>No failure.</summary>
        None,

        /// <summary>Native HUD members, the magnifying-glass prefab, or the owned clone.</summary>
        Component,

        /// <summary>The church target point could not be resolved from the active save.</summary>
        Target,

        /// <summary>The native faith item/icon/sprite could not be borrowed.</summary>
        Icon,

        /// <summary>Native door routing, camera projection, screen bounds or edge geometry.</summary>
        Projection,

        /// <summary>An unexpected error inside this module, contained by the entry point.</summary>
        Internal
    }

    /// <summary>Outcome of one <see cref="ChurchBeacon.Update"/> call.</summary>
    internal readonly struct BeaconUpdateResult
    {
        internal BeaconStatus Status { get; }

        /// <summary>Failing stage when <see cref="Status"/> is Failed; otherwise None.</summary>
        internal BeaconFailureStage Failure { get; }

        internal BeaconUpdateResult(BeaconStatus status, BeaconFailureStage failure)
        {
            Status = status;
            Failure = failure;
        }
    }

    /// <summary>
    /// Owns exactly one beacon display: an independent clone of the native HUD's
    /// private magnifying-glass widget, parented under the same native HUD root so
    /// it inherits the real HUD visibility, layering and canvas scale. It is never
    /// registered in the native quest/work tracking, pool, tracking collections or
    /// priorities, and it never mutates the shared prefab.
    ///
    /// The beacon points at the church sermon scene point
    /// <c>gd_church_player_pray</c>, routed through the native door graph and placed
    /// with the native screen projection, screen bounds, border inset and edge
    /// geometry (the HUD's own private members are invoked by reflection so no
    /// decompiled algorithm is copied). The icon is the native static faith item's
    /// icon borrowed from the native sprite collection: only the Sprite reference is
    /// borrowed, nothing is released or copied, and no substitute icon or fake
    /// Addressables GUID is ever used.
    ///
    /// The native <see cref="UIMagnifyingGlassWidget.Draw"/> swaps the on-screen icon
    /// to the bubble sprite and calls SetNativeSize, so after every Draw the borrowed
    /// faith sprite and the captured pristine prefab slot size are reapplied to the
    /// actual owned <c>iconImage</c>. The native arrow is left exactly as Draw leaves
    /// it (it disappears when on-screen).
    ///
    /// Every entry point contains recoverable exceptions: an eligible failure clears
    /// the owned display, records one bounded failure episode and retries no sooner
    /// than <see cref="RetryDelaySeconds"/> of unscaled time. Being ineligible or
    /// <see cref="Reset"/> clears the owned display synchronously, before any throttle,
    /// so a consumed sermon drops the beacon on the first LateUpdate regardless of the
    /// corner Done presentation.
    /// </summary>
    internal sealed class ChurchBeacon
    {
        /// <summary>Exact native church sermon scene point id.</summary>
        private const string ChurchTargetId = "gd_church_player_pray";

        // Bounded retry backoff measured in unscaled time.
        private const float RetryDelaySeconds = 2f;

        // Native HUD members used for parity. Missing members are a resource fault.
        private const string PrefabFieldName = "magnifyingGlassWidgetPrefab";
        private const string ClampInsetFieldName = "clampWidgetOffsetFromScreenBorder";
        private const string RectEdgeMethodName = "GetRectEdgeIntersection";
        private const string PointerDirectionMethodName = "GetPointerDirectionFromEdgePosition";

        private const string ViewGlobalMethodName = "GetWgoViewGlobal";
        private const string GameSceneTypeName = "GameScene";

        // Cached reflection lookups (build-independent, resolved at most once).
        private bool membersResolved;
        private bool membersAvailable;
        private FieldInfo prefabField;
        private FieldInfo clampInsetField;
        private MethodInfo rectEdgeMethod;
        private MethodInfo pointerDirectionMethod;

        // GameScene derives from an Odin inspector base (Sirenix.Serialization) this
        // project does not reference, so its native static view lookup is resolved by
        // name rather than by naming the type and forcing an unrelated reference.
        private bool viewGlobalResolved;
        private MethodInfo viewGlobalMethod;

        // Explicit ownership of the cloned widget: the flag survives external Unity
        // destruction, while the CLR references identify the binding it came from.
        private bool bindingActive;
        private HUD boundHud;
        private UIMagnifyingGlassWidget boundPrefab;
        private UIMagnifyingGlassWidget widget;
        private Vector2 originalIconSize;

        // One bounded failure episode, independent of the corner-Done presentation.
        private bool failed;
        private BeaconFailureStage failureStage;
        private string lastFailure;
        private float nextRetryUnscaledTime;

        /// <summary>Last failure reason, or null while healthy / inactive.</summary>
        internal string LastFailure => lastFailure;

        /// <summary>
        /// Advance the beacon once per LateUpdate.
        ///
        /// <paramref name="eligible"/> is the owner's business snapshot (gates open,
        /// sermon day, opportunity unconsumed, switch on) and is independent of any
        /// corner rendering, so clearing is never delayed by a pending Done icon.
        /// <paramref name="save"/> is the authoritative active save to read the church
        /// target from; it is never stored beyond the call.
        /// </summary>
        internal BeaconUpdateResult Update(GameSave save, bool eligible)
        {
            try
            {
                return UpdateCore(save, eligible);
            }
            catch (Exception ex)
            {
                // Not attributable to a specific native dependency: drop everything we
                // own, record a bounded internal fault, and never fault the caller.
                ClearOwnedDisplay();
                MarkFailed(BeaconFailureStage.Internal, ex.GetType().Name);
                return new BeaconUpdateResult(BeaconStatus.Failed, BeaconFailureStage.Internal);
            }
        }

        /// <summary>
        /// Release the owned widget and clear the failure episode. Used on exit, load
        /// replacement, switch and teardown so no stale display or reference survives.
        /// </summary>
        internal void Reset()
        {
            try
            {
                ClearOwnedDisplay();
            }
            catch (Exception)
            {
                // A destroyed clone has no state left to clean; nothing further to undo.
            }

            ClearEpisode();
        }

        private BeaconUpdateResult UpdateCore(GameSave save, bool eligible)
        {
            // 1. Not eligible: the owned display is removed synchronously, before any
            //    retry throttle, and the episode ends so a later eligibility never
            //    inherits this load's fault or cooldown.
            if (!eligible)
            {
                ClearOwnedDisplay();
                ClearEpisode();
                return Inactive();
            }

            // 2. A live fault episode defers all resource work until the bounded retry
            //    window opens. An unreadable clock suspends retries without fabricating
            //    a time value; the next readable clock opens the window.
            if (failed)
            {
                if (!TryUnscaledTime(out float now))
                    return new BeaconUpdateResult(BeaconStatus.Failed, failureStage);

                if (now < nextRetryUnscaledTime)
                    return new BeaconUpdateResult(BeaconStatus.Failed, failureStage);

                ClearEpisode();
            }

            if (!EnsureMembers())
                return Fail(BeaconFailureStage.Component, "native HUD beacon members unavailable");

            // 3. Resolve the real native HUD. Absent is ordinary unreadiness (menu,
            //    loading, rebuild), never a resource fault.
            HUD hud = TryGetHud();
            if (hud == null)
            {
                ClearOwnedDisplay();
                return Pending();
            }

            UIMagnifyingGlassWidget prefab = ReadPrefab(hud);
            if (prefab == null)
                return Fail(BeaconFailureStage.Component, "native magnifying glass prefab unavailable");

            // 4. Replacement/destroyed binding detection: explicit ownership plus host
            //    identity, and separately a Unity fake-null owned clone. Unity's
            //    fake-null is never the only signal.
            if (bindingActive
                && (!ReferenceEquals(boundHud, hud)
                    || !ReferenceEquals(boundPrefab, prefab)
                    || widget == null))
            {
                ClearOwnedDisplay();
            }

            if (widget == null && !TryCreateWidget(hud, prefab))
                return Fail(BeaconFailureStage.Component, "beacon widget creation failed");

            // 5. Resolve the exact church target from the authoritative save.
            GDPointData point;
            try
            {
                point = ResolveTarget(save);
            }
            catch (Exception ex)
            {
                return Fail(BeaconFailureStage.Target, "target read failed: " + ex.GetType().Name);
            }

            if (point == null)
                return Fail(BeaconFailureStage.Target, "church target point unavailable");

            // A live camera is native tracking's own precondition; without one it simply
            // skips. Treat a missing camera as ordinary unreadiness and retry next tick.
            if (CameraSystem.Instance == null)
            {
                ClearOwnedDisplay();
                return Pending();
            }

            // 6. Native route + native projection/edge geometry.
            if (!TryBuildWidgetData(hud, point, out UIMagnifyingGlassWidgetData data, out string geometryFailure))
                return Fail(BeaconFailureStage.Projection, geometryFailure);

            // 7. Borrow the native static faith icon.
            Sprite faith = ResolveFaithSprite(out string iconFailure);
            if (faith == null)
                return Fail(BeaconFailureStage.Icon, iconFailure);

            // 8. Draw with the real native widget, then restore the borrowed icon and
            //    the pristine prefab slot the native Draw replaces.
            if (!TryDraw(data, faith))
                return Fail(BeaconFailureStage.Component, "beacon draw failed");

            ClearEpisode();
            return new BeaconUpdateResult(BeaconStatus.Healthy, BeaconFailureStage.None);
        }

        private static BeaconUpdateResult Inactive() =>
            new BeaconUpdateResult(BeaconStatus.Inactive, BeaconFailureStage.None);

        private static BeaconUpdateResult Pending() =>
            new BeaconUpdateResult(BeaconStatus.Pending, BeaconFailureStage.None);

        private BeaconUpdateResult Fail(BeaconFailureStage stage, string reason)
        {
            // Every failure removes the owned display before the caller continues, so
            // no stale or partial beacon can survive.
            ClearOwnedDisplay();
            MarkFailed(stage, reason);
            return new BeaconUpdateResult(BeaconStatus.Failed, stage);
        }

        private bool EnsureMembers()
        {
            if (membersResolved)
                return membersAvailable;

            try
            {
                prefabField = AccessTools.Field(typeof(HUD), PrefabFieldName);
                clampInsetField = AccessTools.Field(typeof(HUD), ClampInsetFieldName);
                rectEdgeMethod = AccessTools.Method(
                    typeof(HUD), RectEdgeMethodName,
                    new[] { typeof(Vector2), typeof(Vector2), typeof(Rect) });
                pointerDirectionMethod = AccessTools.Method(
                    typeof(HUD), PointerDirectionMethodName,
                    new[] { typeof(Vector2), typeof(Rect) });
            }
            catch (Exception)
            {
                // Leave the members null; the beacon hides gracefully on an unknown build.
                prefabField = null;
                clampInsetField = null;
                rectEdgeMethod = null;
                pointerDirectionMethod = null;
            }

            membersAvailable = prefabField != null
                && clampInsetField != null
                && rectEdgeMethod != null
                && pointerDirectionMethod != null;
            membersResolved = true;
            return membersAvailable;
        }

        private static HUD TryGetHud()
        {
            try
            {
                return LazyUI.Get<HUD>();
            }
            catch (Exception)
            {
                return null;
            }
        }

        private UIMagnifyingGlassWidget ReadPrefab(HUD hud)
        {
            if (prefabField == null) return null;
            try
            {
                return prefabField.GetValue(hud) as UIMagnifyingGlassWidget;
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>
        /// Create the independent clone under the same native HUD parent and capture
        /// the pristine prefab slot size before any Draw can replace it.
        /// </summary>
        private bool TryCreateWidget(HUD hud, UIMagnifyingGlassWidget prefab)
        {
            GameObject cloneObject = null;
            try
            {
                cloneObject = UnityEngine.Object.Instantiate(prefab.gameObject, hud.transform);
                if (cloneObject == null)
                    return false;

                UIMagnifyingGlassWidget created = cloneObject.GetComponent<UIMagnifyingGlassWidget>();
                if (created == null)
                {
                    UnityEngine.Object.Destroy(cloneObject);
                    return false;
                }

                // Register ownership immediately so a failure below can never orphan it.
                widget = created;
                bindingActive = true;
                boundHud = hud;
                boundPrefab = prefab;

                Image icon = created.iconImage;
                Image pointer = created.pointerImage;
                if (icon == null || pointer == null)
                {
                    ClearOwnedDisplay();
                    return false;
                }

                // The pristine native prefab slot, captured before any Draw swaps the
                // on-screen bubble icon and calls SetNativeSize.
                Vector2 slot = prefab.iconImage.rectTransform.sizeDelta;
                if (!IsFinite(slot.x) || !IsFinite(slot.y))
                {
                    ClearOwnedDisplay();
                    return false;
                }

                originalIconSize = slot;

                // Stay hidden until a healthy Draw, so a rebuild never flashes a
                // half-prepared widget.
                created.gameObject.SetActive(false);
                return true;
            }
            catch (Exception)
            {
                if (cloneObject != null)
                    UnityEngine.Object.Destroy(cloneObject);

                ClearOwnedDisplay();
                return false;
            }
        }

        private static GDPointData ResolveTarget(GameSave save)
        {
            if (save == null) return null;

            WorldData world = save.worldData;
            if (world == null) return null;

            GdPointsData points = world.gdPointsData;
            if (points == null) return null;

            // Plural API returns the live list without the singular lookup warning.
            // The list is only read, never mutated, and no CustomTag is invented.
            List<GDPointData> candidates = points.GetGDPointsDataById(ChurchTargetId);
            if (candidates == null) return null;

            for (int i = 0; i < candidates.Count; i++)
            {
                GDPointData candidate = candidates[i];
                if (candidate != null)
                    return candidate;
            }

            return null;
        }

        /// <summary>
        /// Resolve the native routing target and screen placement. Returns false with
        /// a stage reason when a native dependency or a finite result is unavailable.
        /// </summary>
        private bool TryBuildWidgetData(
            HUD hud,
            GDPointData point,
            out UIMagnifyingGlassWidgetData data,
            out string failure)
        {
            data = null;
            failure = null;

            Vector3 worldPosition;
            try
            {
                worldPosition = ResolveRoutedWorldPosition(point);
            }
            catch (Exception ex)
            {
                failure = "route failed: " + ex.GetType().Name;
                return false;
            }

            Vector2 rawScreen;
            bool isOutOfScreen;
            Vector2 screenPosition;
            Vector2 direction;
            try
            {
                Vector3 raw = CameraSystem.WorldToScreenPoint(worldPosition);
                if (!IsFinite(raw.x) || !IsFinite(raw.y))
                {
                    failure = "non-finite projection";
                    return false;
                }

                rawScreen = new Vector2(raw.x, raw.y);

                float inset = (float)clampInsetField.GetValue(hud);
                if (!IsFinite(inset))
                {
                    failure = "non-finite border inset";
                    return false;
                }

                // Exact native clamp rect: screen bounds inset by the HUD's own border
                // offset, with the native center as the edge-intersection origin.
                Bounds screenBounds = LazyUI.GetScreenBounds();
                Vector2 center = new Vector2(screenBounds.center.x, screenBounds.center.y);
                Rect clampRect = new Rect(
                    screenBounds.min.x + inset,
                    screenBounds.min.y + inset,
                    screenBounds.size.x - inset * 2f,
                    screenBounds.size.y - inset * 2f);

                isOutOfScreen = !clampRect.Contains(rawScreen);
                if (isOutOfScreen)
                {
                    // The HUD's own geometry, invoked by reflection so the algorithm
                    // and its constants are never copied.
                    screenPosition = InvokeRectEdgeIntersection(hud, center, rawScreen, clampRect);
                    direction = InvokePointerDirectionFromEdgePosition(screenPosition, clampRect);
                }
                else
                {
                    screenPosition = rawScreen;
                    direction = Vector2.zero;
                }
            }
            catch (Exception ex)
            {
                failure = "projection failed: " + ex.GetType().Name;
                return false;
            }

            if (!IsFinite(screenPosition.x) || !IsFinite(screenPosition.y)
                || !IsFinite(direction.x) || !IsFinite(direction.y))
            {
                failure = "non-finite edge geometry";
                return false;
            }

            // Raw on-screen coordinates may be negative; they are passed through
            // unchanged, exactly as the native widget data carries them.
            data = new UIMagnifyingGlassWidgetData(SGuid.Empty, direction, screenPosition)
            {
                IsOutOfScreen = isOutOfScreen
            };
            return true;
        }

        /// <summary>
        /// Native routing: resolve the door the player must cross toward the church and
        /// use the door's bubble position, otherwise the church point itself. Mirrors
        /// the HUD's own <c>TryResolveDoor</c> + <c>GetWgoViewGlobal(...).BubbleDrawablePosition</c>
        /// fallback chain.
        /// </summary>
        private Vector3 ResolveRoutedWorldPosition(GDPointData point)
        {
            Vector3 target = point.Position;

            Vector3 playerPosition = TryGetPlayerPosition(point.Position);

            TeleportPointGraph graph = TeleportPointGraph.Instance;
            if (graph == null)
                return target;

            if (graph.TryResolveDoor(playerPosition, point.Position, out WgoData door) && door != null)
            {
                // Native identity check uses Unity's overloaded null comparison, not
                // the null-conditional shortcut.
                Wgo view = GetWgoViewGlobal(door.UniqueId);
                return (view != null) ? view.BubbleDrawablePosition : door.BubblePos;
            }

            return target;
        }

        private static Vector3 TryGetPlayerPosition(Vector3 fallback)
        {
            try
            {
                PlayerController player = MainGame.PlayerController;
                if (player != null)
                    return player.transform.position;
            }
            catch (Exception)
            {
                // Without a readable player the native code falls back to the object
                // position; mirror that fallback rather than inventing a source.
            }

            return fallback;
        }

        /// <summary>
        /// Native <c>GameScene.GetWgoViewGlobal(SGuid)</c> resolved by name, exactly as
        /// the HUD calls it, without naming its unreferenced Odin-based type.
        /// </summary>
        private Wgo GetWgoViewGlobal(SGuid uniqueId)
        {
            if (!viewGlobalResolved)
            {
                Type gameSceneType = typeof(MainGame).Assembly.GetType(
                    GameSceneTypeName, throwOnError: false);
                viewGlobalMethod = gameSceneType?.GetMethod(
                    ViewGlobalMethodName,
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new[] { typeof(SGuid) },
                    null);
                viewGlobalResolved = true;
            }

            object result = viewGlobalMethod?.Invoke(null, new object[] { uniqueId });
            return result as Wgo;
        }

        /// <summary>
        /// Borrow the native faith sprite: static item "faith" -> its iconId -> the
        /// native sprite collection. Only the reference is borrowed; the collection,
        /// atlas and sprite are never released, and no substitute is used. First-atlas
        /// resolution is synchronous by native design.
        /// </summary>
        private static Sprite ResolveFaithSprite(out string failure)
        {
            failure = null;
            try
            {
                ItemDef faith = GameBalance.Me.GetData<ItemDef>(ItemDef.FAITH_ITEM_ID);
                if (faith == null)
                {
                    failure = "faith item definition unavailable";
                    return null;
                }

                string iconId = faith.iconId;
                if (string.IsNullOrEmpty(iconId))
                {
                    failure = "faith icon id unavailable";
                    return null;
                }

                EasySpritesCollection collection = LazySingletonSO<EasySpritesCollection>.Instance;
                if (collection == null)
                {
                    failure = "native sprite collection unavailable";
                    return null;
                }

                Sprite sprite = collection.GetSprite(iconId);
                if (sprite == null)
                {
                    failure = "faith sprite unavailable";
                    return null;
                }

                return sprite;
            }
            catch (Exception ex)
            {
                failure = "faith sprite failed: " + ex.GetType().Name;
                return null;
            }
        }

        /// <summary>
        /// Draw with the real native widget, then reapply the borrowed faith sprite and
        /// the captured pristine prefab slot to the actual owned image. Native Draw
        /// swaps to the on-screen bubble sprite with SetNativeSize and restores its own
        /// cached prefab icon off-screen, so this must run after every Draw.
        /// </summary>
        private bool TryDraw(UIMagnifyingGlassWidgetData data, Sprite faith)
        {
            try
            {
                UIMagnifyingGlassWidget current = widget;
                if (current == null)
                    return false;

                // Native placement, icon swap and pointer-style selection.
                current.Draw(data);

                Image icon = current.iconImage;
                if (icon == null)
                    return false;

                icon.sprite = faith;

                // A non-null widget guarantees the pristine prefab slot was captured
                // before this clone's first Draw.
                icon.rectTransform.sizeDelta = originalIconSize;

                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private Vector2 InvokeRectEdgeIntersection(HUD hud, Vector2 from, Vector2 to, Rect rect)
        {
            return (Vector2)rectEdgeMethod.Invoke(hud, new object[] { from, to, rect });
        }

        private Vector2 InvokePointerDirectionFromEdgePosition(Vector2 position, Rect rect)
        {
            return (Vector2)pointerDirectionMethod.Invoke(null, new object[] { position, rect });
        }

        /// <summary>
        /// Detach the borrowed sprite from the live owned image (property level, never
        /// destroying the native Sprite) before destroying the clone, then forget the
        /// binding. Only the clone this module created is ever destroyed.
        /// </summary>
        private void ClearOwnedDisplay()
        {
            UIMagnifyingGlassWidget current = widget;
            widget = null;

            if (current != null)
            {
                try
                {
                    Image icon = current.iconImage;
                    if (icon != null)
                        icon.sprite = null;
                }
                catch (Exception)
                {
                    // An already-destroyed component has no property left to clear.
                }

                try
                {
                    GameObject ownedObject = current.gameObject;
                    if (ownedObject != null)
                    {
                        ownedObject.SetActive(false);
                        UnityEngine.Object.Destroy(ownedObject);
                    }
                }
                catch (Exception)
                {
                    // The object below is already gone; nothing to destroy.
                }
            }

            bindingActive = false;
            boundHud = null;
            boundPrefab = null;
            originalIconSize = Vector2.zero;
        }

        private void ClearEpisode()
        {
            failed = false;
            failureStage = BeaconFailureStage.None;
            lastFailure = null;
            nextRetryUnscaledTime = 0f;
        }

        private void MarkFailed(BeaconFailureStage stage, string reason)
        {
            failed = true;
            failureStage = stage;
            lastFailure = reason;

            // Without a readable clock no backoff can be scheduled; reporting the fault
            // and issuing no new work while the clock stays unreadable is the
            // controlled degradation.
            nextRetryUnscaledTime = TryUnscaledTime(out float now) ? now + RetryDelaySeconds : 0f;
        }

        /// <summary>
        /// Read the unscaled clock. Returns false when the native clock cannot be read,
        /// so no time value is ever fabricated.
        /// </summary>
        private static bool TryUnscaledTime(out float unscaledTime)
        {
            unscaledTime = 0f;
            try
            {
                unscaledTime = Time.unscaledTime;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static bool IsFinite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
    }
}
