using System;
using System.Reflection;
using GK2.SermonReminder.Localization;
using GK2.SermonReminder.State;
using HarmonyLib;
using LazyBearTechnology;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace GK2.SermonReminder.Hud
{
    /// <summary>
    /// Owned HUD availability, separating ordinary asynchronous unreadiness from a
    /// real resource failure instead of conflating the two.
    /// </summary>
    internal enum SermonHudStatus
    {
        /// <summary>Usable: binding resolved and safe to render.</summary>
        Healthy,

        /// <summary>Not usable yet (loading/menu/rebuild), or a render dependency is unavailable; ordinary, never a fault.</summary>
        Pending,

        /// <summary>Unusable native member, style source or canvas: a resource failure.</summary>
        Failed
    }

    /// <summary>Outcome of the binding phase.</summary>
    internal readonly struct SermonBindingResult
    {
        internal SermonHudStatus Status { get; }
        internal string Failure { get; }

        /// <summary>
        /// True when a previously owned display or host binding was destroyed or
        /// replaced during this preparation, so the caller must drop its owned
        /// sprite reference before requesting it for the new binding.
        /// </summary>
        internal bool Rebound { get; }

        internal SermonBindingResult(SermonHudStatus status, string failure, bool rebound)
        {
            Status = status;
            Failure = failure;
            Rebound = rebound;
        }
    }

    /// <summary>Outcome of the render phase.</summary>
    internal readonly struct SermonHudResult
    {
        internal SermonHudStatus Status { get; }
        internal string Failure { get; }

        internal SermonHudResult(SermonHudStatus status, string failure)
        {
            Status = status;
            Failure = failure;
        }
    }

    /// <summary>
    /// Owns the single mod-created corner display beneath the native clock. The
    /// display root is parented under the native left-up group, so it inherits the
    /// original HUD's visibility and layering; it never creates its own Canvas or
    /// sorting order.
    ///
    /// The work is split into two phases. <see cref="PrepareBinding"/> resolves and
    /// validates the native host, detects a replaced or destroyed binding, and
    /// applies the native-derived style; only after it reports Healthy may the
    /// caller poll the owned sprite and call <see cref="Update"/>. This ordering is
    /// what keeps a sprite released for a replaced binding from being assigned to
    /// the new display.
    ///
    /// The root always carries exactly one owned TMP text child. One
    /// UnityEngine.UI.Image is created only while the Done state needs it and the
    /// sprite is actually resolved, placed before the Done text, and destroyed when
    /// leaving Done. Countdown and Ready reserve zero icon space and own no Image.
    ///
    /// Styling is fail-closed: every failure path removes the owned presentation
    /// synchronously, so a partial, stale or substitute label is never left visible
    /// while the caller keeps running.
    /// </summary>
    internal sealed class SermonHudAdapter
    {
        private const string DisplayObjectName = "GK2SermonReminder.Display";
        private const string TextObjectName = "GK2SermonReminder.Text";
        private const string IconObjectName = "GK2SermonReminder.DoneIcon";
        private const string HappinessLabelFieldName = "happinessLabel";
        private const string WheelFieldName = "wheel";
        private const string LeftUpGroupFieldName = "leftUpGroup";

        // Small local UI-unit gap; parent-local units scale with the native canvas.
        private const float LocalGap = 6f;

        // Fixed native done-check sprite aspect.
        private const float IconAspectWidth = 20f;
        private const float IconAspectHeight = 18f;

        private bool fieldsResolved;
        private FieldInfo happinessLabelField;
        private FieldInfo wheelField;
        private FieldInfo leftUpGroupField;

        // Explicit ownership of a created binding: the flag survives external Unity
        // destruction, while the CLR references identify the host instance.
        private bool bindingActive;
        private HUD boundHud;
        private GameObject boundGroup;
        private TextMeshProUGUI boundStyleSource;

        private GameObject displayObject;
        private GameObject textObject;
        private TextMeshProUGUI label;
        private GameObject iconObject;
        private Image icon;
        private Material ownedMaterial;

        // Geometry captured during a healthy binding preparation.
        private RectTransform boundParentRect;
        private RectTransform boundWheelRect;
        private float canvasMinX;
        private float canvasMaxX;

        // Layout cache.
        private string appliedText;
        private float appliedAvailable = -1f;
        private SermonDisplayState appliedState = SermonDisplayState.Hidden;
        private Sprite appliedIcon;

        // Style cache.
        private bool styleDirty;
        private bool styleApplied;
        private string appliedLanguage;
        private TMP_FontAsset appliedFont;
        private Material appliedSharedMaterial;
        private float appliedFontSize;
        private FontStyles appliedFontStyle;
        private Color appliedColor;
        private float appliedOutlineWidth;
        private Color32 appliedOutlineColor;

        private bool reboundThisPrepare;

        internal void MarkStyleDirty() => styleDirty = true;

        /// <summary>
        /// Resolve and validate the native host and style before any sprite work.
        /// A previously owned binding that was replaced or destroyed is destroyed
        /// here and reported through <see cref="SermonBindingResult.Rebound"/>, so
        /// the caller can release the old sprite reference before acquiring one for
        /// the new binding.
        /// </summary>
        internal SermonBindingResult PrepareBinding()
        {
            reboundThisPrepare = false;

            if (!EnsureFields())
                return FailBinding("native HUD members unavailable");

            HUD hud;
            try
            {
                hud = LazyUI.Get<HUD>();
            }
            catch (Exception ex)
            {
                // A real native lookup failure is a resource fault, not ordinary
                // pending unreadiness: fail closed with a stable technical reason and
                // never let the native exception escape this tick. A genuine null
                // below still follows the ordinary pending/rebound path.
                return FailBinding("native HUD lookup: " + ex.GetType().Name);
            }

            if (hud == null)
            {
                // The native HUD is simply not live right now (menu, loading, or a
                // rebuild): an ordinary pending condition, never a resource fault. A
                // previously owned binding is gone, so the caller is told to drop
                // the old sprite reference before acquiring one for the new binding.
                if (DestroyOwnedDisplay())
                    reboundThisPrepare = true;

                return new SermonBindingResult(SermonHudStatus.Pending, null, reboundThisPrepare);
            }

            GameObject group = ReadMember<GameObject>(leftUpGroupField, hud);
            TextMeshProUGUI source = ReadMember<TextMeshProUGUI>(happinessLabelField, hud);
            UIHUDWheel wheel = ReadMember<UIHUDWheel>(wheelField, hud);

            // A live HUD with a missing or destroyed required member is a real
            // resource fault, cleaned up before the caller continues. A normally
            // inactive native group is not a failure: a member only has to be
            // present and readable, never active.
            if (group == null || source == null || wheel == null)
                return FailBinding("native HUD members unavailable");

            // Replacement detection: an explicit ownership flag plus host reference
            // identity, and separately a Unity-destroyed owned root. Unity's
            // fake-null is never the only signal.
            bool hostReplaced = bindingActive
                && (!ReferenceEquals(boundHud, hud)
                    || !ReferenceEquals(boundGroup, group)
                    || !ReferenceEquals(boundStyleSource, source));
            bool displayDestroyed = bindingActive && displayObject == null;

            if (hostReplaced || displayDestroyed)
            {
                if (DestroyOwnedDisplay())
                    reboundThisPrepare = true;
            }

            if (displayObject == null && !TryCreateDisplay(hud, group, source))
                return FailBinding("display creation failed");

            string language = SermonReminderLocalization.CurrentLanguageId;
            if (!styleApplied || styleDirty || !string.Equals(appliedLanguage, language, StringComparison.Ordinal)
                || SourceStyleChanged(source))
            {
                if (!ApplyStyle(source, language))
                {
                    // Missing/unreadable style source: never keep a partial or
                    // substitute style. Destroy the display and retry later.
                    return FailBinding("native style source unavailable");
                }
            }

            var parentRect = group.transform as RectTransform;
            var wheelRect = wheel.transform as RectTransform;
            if (parentRect == null || wheelRect == null)
                return FailBinding("native rect transforms unavailable");

            if (!TryGetCanvasLocalBounds(parentRect, out float minX, out float maxX))
            {
                // No resolvable canvas bounds: a resource failure, not a reason to
                // invent placeholder dimensions. Fail closed and retry later.
                return FailBinding("native canvas bounds unavailable");
            }

            boundParentRect = parentRect;
            boundWheelRect = wheelRect;
            canvasMinX = minX;
            canvasMaxX = maxX;
            return new SermonBindingResult(SermonHudStatus.Healthy, null, reboundThisPrepare);
        }

        /// <summary>
        /// Render the given display state with its already-resolved text. Only valid
        /// after <see cref="PrepareBinding"/> reported Healthy.
        ///
        /// A non-null <paramref name="iconSprite"/> with state Done presents the icon
        /// row; a null sprite with state Done suppresses the whole dependent Done
        /// presentation instead of falling back to Done without its icon.
        /// </summary>
        internal SermonHudResult Update(SermonDisplayState state, string text, Sprite iconSprite)
        {
            if (displayObject == null || label == null || textObject == null)
                return FailRender("binding not prepared");

            if (boundParentRect == null || boundWheelRect == null)
                return FailRender("native rect transforms unavailable");

            bool done = state == SermonDisplayState.Done;
            if (done && iconSprite == null)
            {
                Suppress();
                return new SermonHudResult(SermonHudStatus.Pending, "done icon unavailable");
            }

            float available = (canvasMaxX - canvasMinX) - LocalGap * 2f;
            if (!IsPositiveFinite(available))
                return FailRender("canvas width unusable");

            bool layoutCurrent = displayObject.activeSelf
                && appliedState == state
                && ReferenceEquals(appliedIcon, iconSprite)
                && Mathf.Approximately(appliedAvailable, available)
                && string.Equals(appliedText, text, StringComparison.Ordinal);

            if (!layoutCurrent)
            {
                bool laidOut = done
                    ? LayoutWithIcon(text, iconSprite, available)
                    : LayoutWithoutIcon(state, text, available);

                if (!laidOut)
                    return FailRender("native text metrics unavailable");
            }

            PositionDisplay();
            displayObject.SetActive(true);
            return new SermonHudResult(SermonHudStatus.Healthy, null);
        }

        /// <summary>
        /// Detach the owned conditional Image's sprite reference right now, without
        /// tearing anything else down. Used immediately before the caller releases its
        /// sprite handle on a path where the display has not been re-rendered yet (an
        /// off-sermon-day rollover), so the handle is never released while a live
        /// Image still references the sprite it belonged to.
        /// </summary>
        internal void DetachOwnedIcon() => DestroyOwnedIcon();

        /// <summary>
        /// Suppress the presentation while a dependency is unavailable: remove the
        /// conditional icon and hide the label, but keep the binding and the cached
        /// style so a later tick can render again without a rebind.
        /// </summary>
        internal void Suppress()
        {
            DestroyOwnedIcon();
            ClearOwnedText();

            if (displayObject != null)
                displayObject.SetActive(false);

            ResetLayoutCache();
        }

        /// <summary>
        /// Reverse everything created and release the owned material. The caller
        /// owns and releases the sprite handle.
        /// </summary>
        internal void Teardown()
        {
            DestroyOwnedDisplay();
            styleDirty = false;
        }

        /// <summary>Countdown/Ready: full available width, original Top alignment, no icon and no reserved space.</summary>
        private bool LayoutWithoutIcon(SermonDisplayState state, string text, float available)
        {
            DestroyOwnedIcon();
            label.alignment = TextAlignmentOptions.Top;
            label.textWrappingMode = TextWrappingModes.Normal;

            Vector2 preferred = label.GetPreferredValues(text, available, 0f);
            if (!IsPositiveFinite(preferred.x) || !IsPositiveFinite(preferred.y))
                return false;

            float width = Mathf.Clamp(preferred.x, 1f, available);
            ApplyTextLayout(text, 0f, width, preferred.y);
            ApplyDisplaySize(width, preferred.y);
            RememberLayout(state, text, available, null);
            return true;
        }

        /// <summary>
        /// Done: place the icon first and the text beside it, sizing the icon from
        /// the actual localized Done sentence's unwrapped line height. The combined
        /// row is refused when it cannot fit the real canvas width.
        /// </summary>
        private bool LayoutWithIcon(string text, Sprite sprite, float available)
        {
            // Done reads as an icon row, so its text starts at the row's left edge.
            label.alignment = TextAlignmentOptions.TopLeft;
            label.textWrappingMode = TextWrappingModes.Normal;

            // Measure the actual Done sentence unwrapped: this is the real single
            // line height, never a probe glyph or a wrapped paragraph height.
            Vector2 unwrapped = label.GetPreferredValues(text, float.PositiveInfinity, 0f);
            if (!IsPositiveFinite(unwrapped.x) || !IsPositiveFinite(unwrapped.y))
                return false;

            float lineHeight = unwrapped.y;
            float iconHeight = lineHeight;
            float iconWidth = lineHeight * (IconAspectWidth / IconAspectHeight);
            float reserved = iconWidth + LocalGap;

            if (!IsPositiveFinite(iconWidth) || !IsPositiveFinite(iconHeight) || !(reserved < available))
                return false;

            // Wrap only the text layout inside the width left by the icon and gap.
            float remaining = available - reserved;
            Vector2 wrapped = label.GetPreferredValues(text, remaining, 0f);
            if (!IsPositiveFinite(wrapped.x) || !IsPositiveFinite(wrapped.y))
                return false;
            if (wrapped.x > remaining)
                return false;

            float textWidth = wrapped.x;
            float textHeight = wrapped.y;
            float displayWidth = iconWidth + LocalGap + textWidth;
            float displayHeight = Mathf.Max(textHeight, iconHeight);

            // Never extend the row past the real canvas width.
            if (!IsPositiveFinite(displayWidth) || !IsPositiveFinite(displayHeight) || displayWidth > available)
                return false;

            ApplyDisplaySize(displayWidth, displayHeight);
            ApplyTextLayout(text, reserved, textWidth, displayHeight);

            if (iconObject == null && !TryCreateIcon())
                return false;

            if (!ApplyIconSpriteAndStyle(sprite))
            {
                DestroyOwnedIcon();
                return false;
            }

            var iconRect = (RectTransform)iconObject.transform;
            iconRect.anchoredPosition = Vector2.zero;
            iconRect.sizeDelta = new Vector2(iconWidth, iconHeight);

            RememberLayout(SermonDisplayState.Done, text, available, sprite);
            return true;
        }

        private SermonBindingResult FailBinding(string reason)
        {
            // Every real binding failure removes the owned presentation before the
            // caller can continue, so no stale label survives. If a binding was
            // actually destroyed, the caller is told to drop the old sprite
            // reference so it can never be left orphaned.
            if (DestroyOwnedDisplay())
                reboundThisPrepare = true;

            return new SermonBindingResult(SermonHudStatus.Failed, reason, reboundThisPrepare);
        }

        private SermonHudResult FailRender(string reason)
        {
            // A render that cannot produce a valid layout hides the owned
            // presentation (removing the conditional Image and clearing the cached
            // text) while keeping the binding so a later tick can retry.
            Suppress();
            return new SermonHudResult(SermonHudStatus.Failed, reason);
        }

        private bool EnsureFields()
        {
            if (fieldsResolved)
                return happinessLabelField != null && wheelField != null && leftUpGroupField != null;

            try
            {
                happinessLabelField = AccessTools.Field(typeof(HUD), HappinessLabelFieldName);
                wheelField = AccessTools.Field(typeof(HUD), WheelFieldName);
                leftUpGroupField = AccessTools.Field(typeof(HUD), LeftUpGroupFieldName);
            }
            catch (Exception)
            {
                // Leave the fields null; the mod hides gracefully on an unknown build.
            }

            fieldsResolved = true;
            return happinessLabelField != null && wheelField != null && leftUpGroupField != null;
        }

        private static T ReadMember<T>(FieldInfo field, object target) where T : class
        {
            if (field == null) return null;
            try
            {
                return field.GetValue(target) as T;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private bool TryCreateDisplay(HUD hud, GameObject group, TextMeshProUGUI source)
        {
            GameObject root = null;
            GameObject textGo = null;
            try
            {
                root = new GameObject(DisplayObjectName, typeof(RectTransform));

                // Register both objects for cleanup immediately: if parenting or
                // component creation fails, neither can be orphaned.
                displayObject = root;
                bindingActive = true;

                var rootRect = (RectTransform)root.transform;
                rootRect.anchorMin = new Vector2(0f, 1f);
                rootRect.anchorMax = new Vector2(0f, 1f);
                rootRect.pivot = new Vector2(0.5f, 1f);

                // Stay hidden until a successful render, so a rebuild never shows
                // an unfinished label.
                root.SetActive(false);
                root.transform.SetParent(group.transform, false);

                textGo = new GameObject(TextObjectName, typeof(RectTransform));
                textObject = textGo;

                textGo.transform.SetParent(root.transform, false);

                var text = textGo.AddComponent<TextMeshProUGUI>();
                text.raycastTarget = false;

                label = text;
                boundHud = hud;
                boundGroup = group;
                boundStyleSource = source;
                styleApplied = false;
                appliedLanguage = null;
                ResetLayoutCache();
                return true;
            }
            catch (Exception)
            {
                if (textGo != null) UnityEngine.Object.Destroy(textGo);
                if (root != null) UnityEngine.Object.Destroy(root);
                displayObject = null;
                textObject = null;
                label = null;
                iconObject = null;
                icon = null;
                bindingActive = false;
                ResetBindingRefs();
                return false;
            }
        }

        private bool TryCreateIcon()
        {
            GameObject go = null;
            try
            {
                go = new GameObject(IconObjectName, typeof(RectTransform));
                iconObject = go;

                // Owned by the display root and forced to sibling index 0, so the
                // Image renders before the Done text child.
                go.transform.SetParent(displayObject.transform, false);
                go.transform.SetSiblingIndex(0);

                var rect = (RectTransform)go.transform;
                rect.anchorMin = new Vector2(0f, 1f);
                rect.anchorMax = new Vector2(0f, 1f);
                rect.pivot = new Vector2(0f, 1f);
                rect.anchoredPosition = Vector2.zero;

                var image = go.AddComponent<Image>();
                image.raycastTarget = false;
                image.preserveAspect = true;
                image.color = Color.white;

                icon = image;
                return true;
            }
            catch (Exception)
            {
                if (go != null) UnityEngine.Object.Destroy(go);
                iconObject = null;
                icon = null;
                return false;
            }
        }

        private bool ApplyIconSpriteAndStyle(Sprite sprite)
        {
            try
            {
                if (icon == null || sprite == null) return false;

                // Use the original Sprite as-is with the default UI material:
                // never cloned, never mutated, and no shared native material is
                // borrowed or replaced.
                icon.sprite = sprite;
                icon.color = Color.white;
                icon.raycastTarget = false;
                icon.preserveAspect = true;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool ApplyStyle(TextMeshProUGUI source, string language)
        {
            try
            {
                TMP_FontAsset font = source.font;
                Material shared = source.fontSharedMaterial;
                if (font == null || shared == null) return false;

                // Read every native value first so a partial read cannot leave a
                // half-styled label behind.
                float fontSize = source.fontSize;
                FontStyles fontStyle = source.fontStyle;
                Color color = source.color;
                float outlineWidth = source.outlineWidth;
                Color32 outlineColor = source.outlineColor;

                label.font = font;

                // Copy the source material into a private instance so outline
                // changes never mutate the shared native material, replacing any
                // instance we already own.
                if (ownedMaterial != null)
                {
                    UnityEngine.Object.Destroy(ownedMaterial);
                    ownedMaterial = null;
                }

                ownedMaterial = new Material(shared);
                label.fontSharedMaterial = ownedMaterial;
                label.fontSize = fontSize;
                label.fontStyle = fontStyle;
                label.color = color;
                label.outlineWidth = outlineWidth;
                label.outlineColor = outlineColor;

                label.textWrappingMode = TextWrappingModes.Normal;
                label.overflowMode = TextOverflowModes.Overflow;
                label.richText = false;
                label.raycastTarget = false;

                appliedFont = font;
                appliedSharedMaterial = shared;
                appliedFontSize = fontSize;
                appliedFontStyle = fontStyle;
                appliedColor = color;
                appliedOutlineWidth = outlineWidth;
                appliedOutlineColor = outlineColor;
                appliedLanguage = language;
                styleApplied = true;
                styleDirty = false;

                // The reapply changed the font metrics, so any cached measurement
                // and icon size are stale.
                ResetLayoutCache();
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// True when the native source's font, size, style, material, color or
        /// outline no longer match what the owned label was styled from, so the
        /// style is reapplied on the next tick.
        /// </summary>
        private bool SourceStyleChanged(TextMeshProUGUI source)
        {
            try
            {
                if (!ReferenceEquals(appliedFont, source.font)) return true;
                if (!ReferenceEquals(appliedSharedMaterial, source.fontSharedMaterial)) return true;
                if (!Mathf.Approximately(appliedFontSize, source.fontSize)) return true;
                if (appliedFontStyle != source.fontStyle) return true;
                if (!(appliedColor == source.color)) return true;
                if (!Mathf.Approximately(appliedOutlineWidth, source.outlineWidth)) return true;
                if (!appliedOutlineColor.Equals(source.outlineColor)) return true;
                return false;
            }
            catch (Exception)
            {
                // Unreadable source: force a reapply, which fails closed if the
                // source is genuinely gone.
                return true;
            }
        }

        private void ApplyTextLayout(string text, float offsetX, float width, float height)
        {
            if (!string.Equals(appliedText, text, StringComparison.Ordinal))
            {
                label.text = text;
                appliedText = text;
            }

            var rect = label.rectTransform;
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0f, 1f);
            rect.anchoredPosition = new Vector2(offsetX, 0f);
            rect.sizeDelta = new Vector2(width, height);
        }

        private void ApplyDisplaySize(float width, float height)
        {
            if (displayObject == null) return;

            var rect = (RectTransform)displayObject.transform;
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.sizeDelta = new Vector2(width, height);
        }

        /// <summary>
        /// Anchor the combined display under the clock and clamp it inside the real
        /// canvas width. Anchors are (0,1); anchoredPosition is measured from the
        /// parent's top-left corner in parent-local units.
        /// </summary>
        private void PositionDisplay()
        {
            if (displayObject == null || boundWheelRect == null || boundParentRect == null) return;

            var corners = new Vector3[4];
            boundWheelRect.GetWorldCorners(corners);

            float minX = float.MaxValue;
            float maxX = float.MinValue;
            float minY = float.MaxValue;
            for (int i = 0; i < 4; i++)
            {
                Vector3 local = boundParentRect.InverseTransformPoint(corners[i]);
                if (local.x < minX) minX = local.x;
                if (local.x > maxX) maxX = local.x;
                if (local.y < minY) minY = local.y;
            }

            var rect = (RectTransform)displayObject.transform;
            float half = rect.sizeDelta.x * 0.5f;
            float lowerBound = canvasMinX + half + LocalGap;
            float upperBound = canvasMaxX - half - LocalGap;
            float desiredX = (minX + maxX) * 0.5f;
            float pivotLocalX = lowerBound <= upperBound
                ? Mathf.Clamp(desiredX, lowerBound, upperBound)
                : (canvasMinX + canvasMaxX) * 0.5f;
            float pivotLocalY = minY - LocalGap;

            float anchorLeftX = boundParentRect.rect.xMin;
            float anchorTopY = boundParentRect.rect.yMax;
            rect.anchoredPosition = new Vector2(pivotLocalX - anchorLeftX, pivotLocalY - anchorTopY);
        }

        /// <summary>
        /// Resolve the horizontal bounds of the host canvas expressed in the
        /// parent's local coordinates. Returns false when no real canvas rect is
        /// reachable, so the caller fails closed instead of using invented bounds.
        /// </summary>
        private static bool TryGetCanvasLocalBounds(RectTransform parentRect, out float minX, out float maxX)
        {
            Canvas canvas = parentRect.GetComponentInParent<Canvas>();
            RectTransform canvasRect = canvas != null ? canvas.transform as RectTransform : null;
            if (canvasRect == null)
            {
                minX = 0f;
                maxX = 0f;
                return false;
            }

            // A bounds read that yields non-finite values is not usable.
            if (canvasRect == parentRect)
            {
                // The parent is the canvas itself: its own rect is already the
                // canvas space, so use it directly rather than 0..width.
                minX = parentRect.rect.xMin;
                maxX = parentRect.rect.xMax;
                return IsFinite(minX) && IsFinite(maxX) && maxX > minX;
            }

            var corners = new Vector3[4];
            canvasRect.GetWorldCorners(corners);

            minX = float.MaxValue;
            maxX = float.MinValue;
            for (int i = 0; i < 4; i++)
            {
                Vector3 local = parentRect.InverseTransformPoint(corners[i]);
                if (local.x < minX) minX = local.x;
                if (local.x > maxX) maxX = local.x;
            }

            return IsFinite(minX) && IsFinite(maxX) && maxX > minX;
        }

        private void RememberLayout(SermonDisplayState state, string text, float available, Sprite iconSprite)
        {
            appliedState = state;
            appliedAvailable = available;
            appliedIcon = iconSprite;
        }

        /// <summary>
        /// Clear the owned label's real text so a suppressed label can never keep
        /// showing the previous sentence. No fallback sentence is substituted; the
        /// layout cache is reset by the caller so the next render reassigns it.
        /// </summary>
        private void ClearOwnedText()
        {
            TextMeshProUGUI owned = label;
            if (owned == null) return;

            try
            {
                // Only touch the property while it still holds text, so a label
                // suppressed across many ticks is not rebuilt every frame.
                if (!string.IsNullOrEmpty(owned.text))
                    owned.text = string.Empty;
            }
            catch (Exception)
            {
                // A destroyed label is replaced on the next bind; nothing to clear.
            }
        }

        /// <summary>
        /// Remove the owned conditional Image. The game's shared Sprite is detached
        /// at the property level first (never destroyed: it is not ours), the object
        /// is hidden synchronously, and only then is it handed to Unity's deferred
        /// destruction. Display teardown shares this cleanup instead of duplicating it.
        /// </summary>
        private void DestroyOwnedIcon()
        {
            Image ownedImage = icon;
            if (ownedImage != null)
            {
                try
                {
                    // Clear the actual UnityEngine.UI.Image.sprite property, not
                    // just this C# field, before destruction and before the owner
                    // releases its handle.
                    ownedImage.sprite = null;
                }
                catch (Exception)
                {
                    // An already-destroyed component has no property left to clear;
                    // the object below is still removed.
                }
            }

            icon = null;

            GameObject ownedObject = iconObject;
            iconObject = null;

            if (ownedObject != null)
            {
                ownedObject.SetActive(false);
                UnityEngine.Object.Destroy(ownedObject);
            }
        }

        private bool DestroyOwnedDisplay()
        {
            // Whether an owned binding really existed, so callers can decide if the
            // owned sprite reference must be released.
            bool hadBinding = bindingActive;

            // Share the icon cleanup, so the sprite is detached from the live Image
            // here too rather than by a separate, inconsistent path.
            DestroyOwnedIcon();

            if (textObject != null)
            {
                textObject.SetActive(false);
                UnityEngine.Object.Destroy(textObject);
                textObject = null;
            }

            if (displayObject != null)
            {
                displayObject.SetActive(false);
                UnityEngine.Object.Destroy(displayObject);
                displayObject = null;
            }

            if (ownedMaterial != null)
            {
                UnityEngine.Object.Destroy(ownedMaterial);
                ownedMaterial = null;
            }

            label = null;
            bindingActive = false;
            appliedLanguage = null;
            ResetBindingRefs();
            return hadBinding;
        }

        private void ResetLayoutCache()
        {
            appliedText = null;
            appliedAvailable = -1f;
            appliedState = SermonDisplayState.Hidden;
            appliedIcon = null;
        }

        private void ResetBindingRefs()
        {
            boundHud = null;
            boundGroup = null;
            boundStyleSource = null;
            boundParentRect = null;
            boundWheelRect = null;
            canvasMinX = 0f;
            canvasMaxX = 0f;
            styleApplied = false;
            ResetLayoutCache();
        }

        private static bool IsFinite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);

        private static bool IsPositiveFinite(float value) => IsFinite(value) && value > 0f;
    }
}
