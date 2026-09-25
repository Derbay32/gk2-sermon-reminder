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
    /// Ownership is tracked on the clone's <see cref="GameObject"/> the instant it is
    /// instantiated, plus the actual owned <c>Image</c>, so cleanup still detaches the
    /// borrowed sprite and destroys a live clone even when the widget component has
    /// become Unity fake-null. A failed cleanup keeps ownership for a later retry
    /// rather than dropping the only reference or creating a second instance.
    ///
    /// Every entry point contains recoverable exceptions: an eligible failure clears
    /// the owned display, records one bounded failure episode and retries no sooner
    /// than <see cref="RetryDelaySeconds"/> of unscaled time. The failure metadata is
    /// retained across retries and Pending states and cleared only after a complete
    /// Draw succeeds or at the Reset/ineligible boundary. Being ineligible or
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

        // Explicit ownership of the cloned display. `bindingActive` and the host refs
        // identify the binding; `ownedObject` is the clone GameObject and `ownedIcon`
        // the actual owned Image, both tracked so cleanup survives the widget component
        // becoming Unity fake-null.
        private bool bindingActive;
        private HUD boundHud;
        private UIMagnifyingGlassWidget boundPrefab;
        private GameObject ownedObject;
        private UIMagnifyingGlassWidget widget;
        private Image ownedIcon;
        private Vector2 originalIconSize;
        private bool releasePending;

        // One bounded failure episode, independent of the corner-Done presentation.
        // The metadata is kept across retries/Pending and cleared only on a complete
        // Draw or at the Reset/ineligible boundary.
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
                ReleaseOwnedDisplay();
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
                ReleaseOwnedDisplay();
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
                ReleaseOwnedDisplay();
                ClearEpisode();
                return Inactive();
            }

            // 2. A live fault episode defers all resource work until the bounded retry
            //    window opens. An unreadable/non-finite clock suspends retries without
            //    fabricating a time value. The failure metadata is deliberately NOT
            //    cleared here: it is retained until a complete Draw succeeds so a
            //    diagnostic is never dropped before recovery.
            if (failed)
            {
                if (!TryUnscaledTime(out float now))
                    return new BeaconUpdateResult(BeaconStatus.Failed, failureStage);

                if (now < nextRetryUnscaledTime)
                    return new BeaconUpdateResult(BeaconStatus.Failed, failureStage);
            }

            if (!EnsureMembers())
                return Fail(BeaconFailureStage.Component, "native HUD beacon members unavailable");

            // 3. Resolve the real native HUD. A legitimate null is ordinary unreadiness
            //    (menu, loading, rebuild); a thrown native access is a contained fault.
            if (!TryReadHud(out HUD hud, out string hudFailure))
                return Fail(BeaconFailureStage.Component, hudFailure);

            if (hud == null)
            {
                ReleaseOwnedDisplay();
                return Pending();
            }

            UIMagnifyingGlassWidget prefab = ReadPrefab(hud);
            if (prefab == null)
                return Fail(BeaconFailureStage.Component, "native magnifying glass prefab unavailable");

            // 4. Replacement/destroyed binding detection: explicit ownership plus host
            //    identity, and separately a Unity fake-null owned object or widget. Unity's
            //    fake-null is never the only signal.
            bool hostReplaced = bindingActive
                && (!ReferenceEquals(boundHud, hud) || !ReferenceEquals(boundPrefab, prefab));
            bool ownedGone = bindingActive && ownedObject == null;
            bool componentGone = bindingActive && widget == null;

            // A retained binding is released before any rebuild when the host changed,
            // the owned object/component vanished, or a prior release failed
            // (`releasePending`). A stuck orphan is thus re-attempted on the next
            // bounded retry rather than reused or duplicated; an unrelated fault does
            // not needlessly destroy a healthy owned widget.
            if (bindingActive && (hostReplaced || ownedGone || componentGone || releasePending))
            {
                if (!ReleaseOwnedDisplay())
                {
                    // A still-live owned orphan could not be released: fail and retry
                    // rather than create a second instance alongside it.
                    return Fail(BeaconFailureStage.Component, "owned beacon release failed");
                }
            }

            // 5. Create only when we truly own nothing, so a retained orphan can never
            //    coexist with a replacement.
            if (!bindingActive && !TryCreateWidget(hud, prefab))
                return Fail(BeaconFailureStage.Component, "beacon widget creation failed");

            // 6. Resolve the exact church target from the authoritative save.
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
                ReleaseOwnedDisplay();
                return Pending();
            }

            // 7. Native route + native projection/edge geometry.
            if (!TryBuildWidgetData(hud, point, out UIMagnifyingGlassWidgetData data, out string geometryFailure))
                return Fail(BeaconFailureStage.Projection, geometryFailure);

            // 8. Borrow the native static faith icon.
            Sprite faith = ResolveFaithSprite(out string iconFailure);
            if (faith == null)
                return Fail(BeaconFailureStage.Icon, iconFailure);

            // 9. Draw with the real native widget, then restore the borrowed icon and
            //    the pristine prefab slot the native Draw replaces. This is the only
            //    point at which the failure episode is cleared.
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
            // no stale or partial beacon can survive. A failed release keeps ownership
            // and is retried on the next bounded attempt.
            ReleaseOwnedDisplay();
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

        /// <summary>
        /// Read the live native HUD. A legitimate null is ordinary unreadiness reported
        /// by the caller as Pending; a thrown access is a real contained fault.
        /// </summary>
        private static bool TryReadHud(out HUD hud, out string failure)
        {
            hud = null;
            failure = null;
            try
            {
                hud = LazyUI.Get<HUD>();
                return true;
            }
            catch (Exception ex)
            {
                failure = "native HUD access failed: " + ex.GetType().Name;
                return false;
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
        /// the pristine prefab slot size before any Draw can replace it. The clone
        /// GameObject is owned from the instant it is created, so every failure path
        /// releases through the one ownership-aware cleanup (never a second Destroy).
        /// </summary>
        private bool TryCreateWidget(HUD hud, UIMagnifyingGlassWidget prefab)
        {
            GameObject clone;
            try
            {
                clone = UnityEngine.Object.Instantiate(prefab.gameObject, hud.transform);
            }
            catch (Exception)
            {
                return false;
            }

            if (clone == null)
                return false;

            // Own the GameObject before any later step can fail, so it can never be
            // orphaned by a partial create.
            ownedObject = clone;
            bindingActive = true;
            boundHud = hud;
            boundPrefab = prefab;

            try
            {
                UIMagnifyingGlassWidget created = clone.GetComponent<UIMagnifyingGlassWidget>();
                if (created == null)
                {
                    ReleaseOwnedDisplay();
                    return false;
                }

                widget = created;

                Image icon = created.iconImage;

                // Cache the actual owned image before any further check, so cleanup can
                // detach the borrowed sprite even if another member is missing.
                ownedIcon = icon;

                Image pointer = created.pointerImage;
                if (icon == null || pointer == null)
                {
                    ReleaseOwnedDisplay();
                    return false;
                }

                // The pristine native prefab slot, captured before any Draw swaps the
                // on-screen bubble icon and calls SetNativeSize. It must be strictly
                // positive, not merely finite.
                Vector2 slot = prefab.iconImage.rectTransform.sizeDelta;
                if (!IsPositiveFinite(slot.x) || !IsPositiveFinite(slot.y))
                {
                    ReleaseOwnedDisplay();
                    return false;
                }

                originalIconSize = slot;

                // Stay hidden until a healthy Draw, so a rebuild never flashes a
                // half-prepared widget.
                clone.SetActive(false);
                return true;
            }
            catch (Exception)
            {
                ReleaseOwnedDisplay();
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
        /// a stage reason when a native dependency or a usable finite result is
        /// unavailable; invalid screen bounds never yield a finite fake position.
        /// </summary>
        private bool TryBuildWidgetData(
            HUD hud,
            GDPointData point,
            out UIMagnifyingGlassWidgetData data,
            out string failure)
        {
            data = null;
            failure = null;

            if (!TryResolveRoutedWorldPosition(point, out Vector3 worldPosition, out failure))
                return false;

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

                // The native bounds must be finite and usable before geometry runs, so
                // an invalid viewport cannot produce a finite-looking Healthy position.
                Bounds screenBounds = LazyUI.GetScreenBounds();
                if (!IsFinite(screenBounds.center.x) || !IsFinite(screenBounds.center.y)
                    || !IsPositiveFinite(screenBounds.size.x) || !IsPositiveFinite(screenBounds.size.y))
                {
                    failure = "native screen bounds unusable";
                    return false;
                }

                // Exact native clamp rect: screen bounds inset by the HUD's own border
                // offset, with the native center as the edge-intersection origin.
                Vector2 center = new Vector2(screenBounds.center.x, screenBounds.center.y);
                Rect clampRect = new Rect(
                    screenBounds.min.x + inset,
                    screenBounds.min.y + inset,
                    screenBounds.size.x - inset * 2f,
                    screenBounds.size.y - inset * 2f);

                if (!IsPositiveFinite(clampRect.width) || !IsPositiveFinite(clampRect.height)
                    || !IsFinite(clampRect.x) || !IsFinite(clampRect.y))
                {
                    failure = "native clamp rect unusable";
                    return false;
                }

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
        /// fallback chain. A failed native view lookup is a fault; the door fallback is
        /// used only when the exact native method runs and legitimately returns null.
        /// </summary>
        private bool TryResolveRoutedWorldPosition(GDPointData point, out Vector3 worldPosition, out string failure)
        {
            worldPosition = point.Position;
            failure = null;

            if (!TryGetPlayerPosition(point.Position, out Vector3 playerPosition, out failure))
                return false;

            TeleportPointGraph graph = TeleportPointGraph.Instance;
            if (graph == null)
                return true;

            bool hasDoor;
            WgoData door;
            try
            {
                hasDoor = graph.TryResolveDoor(playerPosition, point.Position, out door) && door != null;
            }
            catch (Exception ex)
            {
                failure = "door route failed: " + ex.GetType().Name;
                return false;
            }

            if (!hasDoor)
                return true;

            if (!TryGetWgoViewGlobal(door.UniqueId, out Wgo view, out failure))
                return false;

            try
            {
                // Only a successfully invoked native lookup that returns actual null /
                // fake-null falls back to the door bubble position.
                worldPosition = (view != null) ? view.BubbleDrawablePosition : door.BubblePos;
            }
            catch (Exception ex)
            {
                failure = "door target position failed: " + ex.GetType().Name;
                return false;
            }

            return true;
        }

        /// <summary>
        /// Read the live player position. A legitimate null player uses the verified
        /// native fallback (the object position); a thrown access is a real fault.
        /// </summary>
        private static bool TryGetPlayerPosition(Vector3 fallback, out Vector3 position, out string failure)
        {
            position = fallback;
            failure = null;
            try
            {
                PlayerController player = MainGame.PlayerController;
                if (player != null)
                    position = player.transform.position;
                return true;
            }
            catch (Exception ex)
            {
                failure = "player access failed: " + ex.GetType().Name;
                return false;
            }
        }

        /// <summary>
        /// Native <c>GameScene.GetWgoViewGlobal(SGuid)</c> resolved by name, exactly as
        /// the HUD calls it, without naming its unreferenced Odin-based type. A missing
        /// type or method is a fault, never silently treated as an absent native view.
        /// </summary>
        private bool TryGetWgoViewGlobal(SGuid uniqueId, out Wgo view, out string failure)
        {
            view = null;
            failure = null;

            if (!viewGlobalResolved)
            {
                viewGlobalMethod = ResolveViewGlobalMethod();
                viewGlobalResolved = true;
            }

            if (viewGlobalMethod == null)
            {
                failure = "native GameScene.GetWgoViewGlobal unavailable";
                return false;
            }

            object result;
            try
            {
                result = viewGlobalMethod.Invoke(null, new object[] { uniqueId });
            }
            catch (Exception ex)
            {
                failure = "native view lookup failed: " + ex.GetType().Name;
                return false;
            }

            // A successful invocation may legitimately yield null / fake-null; that is
            // the native "no view" answer the caller may fall back from.
            view = result as Wgo;
            return true;
        }

        private static MethodInfo ResolveViewGlobalMethod()
        {
            try
            {
                Type gameSceneType = typeof(MainGame).Assembly.GetType(
                    GameSceneTypeName, throwOnError: false);
                if (gameSceneType == null)
                    return null;

                return gameSceneType.GetMethod(
                    ViewGlobalMethodName,
                    BindingFlags.Public | BindingFlags.Static,
                    null,
                    new[] { typeof(SGuid) },
                    null);
            }
            catch (Exception)
            {
                return null;
            }
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

                // Reapply to the actual owned image Draw just touched; fall back to the
                // tracked reference if the live field has become unusable.
                Image icon = current.iconImage;
                if (icon == null)
                    icon = ownedIcon;

                if (icon == null)
                    return false;

                icon.sprite = faith;

                // A live owned image guarantees the pristine prefab slot was captured
                // and validated strictly positive before this clone's first Draw.
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
        /// Release the owned display: detach the borrowed sprite from the actual owned
        /// image, deactivate and destroy the owned clone, and forget the binding. This
        /// runs even when the widget component is Unity fake-null, because the clone
        /// GameObject and image are tracked separately. A genuinely destroyed object is
        /// forgotten; a failed cleanup of a still-live object keeps ownership so a later
        /// retry can finish and no duplicate is created. Only our clone is ever
        /// destroyed; no prefab or native resource is touched.
        /// </summary>
        /// <returns>True when nothing owned remains; false when a live owned object could not be released.</returns>
        private bool ReleaseOwnedDisplay()
        {
            // Detach the borrowed sprite at the property level first, never destroying
            // the native Sprite. Fall back to the live widget when the cached image is
            // not available.
            Image icon = ownedIcon;
            if (icon == null)
            {
                UIMagnifyingGlassWidget live = widget;
                if (live != null)
                {
                    try
                    {
                        icon = live.iconImage;
                    }
                    catch (Exception)
                    {
                        icon = null;
                    }
                }
            }

            if (icon != null)
            {
                try
                {
                    icon.sprite = null;
                }
                catch (Exception)
                {
                    // An already-destroyed component has no property left to clear.
                }
            }

            GameObject owned = ownedObject;

            if (owned == null)
            {
                // Genuinely gone (never created, or destroyed externally): release all
                // ownership references so a replacement can be built cleanly.
                ForgetOwnership();
                return true;
            }

            try
            {
                owned.SetActive(false);
                UnityEngine.Object.Destroy(owned);
            }
            catch (Exception)
            {
                // Could not release a still-live orphan: keep ownership so a later tick
                // retries instead of losing the reference or creating a duplicate.
                releasePending = true;
                return false;
            }

            ForgetOwnership();
            return true;
        }

        private void ForgetOwnership()
        {
            bindingActive = false;
            boundHud = null;
            boundPrefab = null;
            ownedObject = null;
            widget = null;
            ownedIcon = null;
            originalIconSize = Vector2.zero;
            releasePending = false;
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
        /// Read the unscaled clock. Returns false when the native clock cannot be read
        /// or is non-finite, so no time value is ever fabricated and a NaN/Infinity
        /// deadline can never satisfy the retry window.
        /// </summary>
        private static bool TryUnscaledTime(out float unscaledTime)
        {
            unscaledTime = 0f;
            try
            {
                float now = Time.unscaledTime;
                if (!IsFinite(now))
                    return false;

                unscaledTime = now;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static bool IsFinite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);

        private static bool IsPositiveFinite(float value) => IsFinite(value) && value > 0f;
    }
}
