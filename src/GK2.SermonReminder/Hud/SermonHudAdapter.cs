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
        /// <summary>Bound and current (or deliberately suppressed while an icon load is pending).</summary>
        Healthy,

        /// <summary>Native HUD not usable yet (loading/menu/rebuild); ordinary, never a fault.</summary>
        Pending,

        /// <summary>Unusable native style source or canvas: a resource failure.</summary>
        Failed
    }

    /// <summary>Outcome of one HUD update.</summary>
    internal readonly struct SermonHudResult
    {
        internal SermonHudStatus Status { get; }
        internal string Failure { get; }

        /// <summary>
        /// True when the native HUD was rebuilt/rebound during this update, so the
        /// caller must drop its owned sprite reference and let the new binding
        /// request it again.
        /// </summary>
        internal bool Rebound { get; }

        internal SermonHudResult(SermonHudStatus status, string failure, bool rebound)
        {
            Status = status;
            Failure = failure;
            Rebound = rebound;
        }
    }

    /// <summary>
    /// Owns the single mod-created corner display beneath the native clock. The
    /// display root is parented under the native left-up group, so it inherits the
    /// original HUD's visibility and layering; it never creates its own Canvas or
    /// sorting order.
    ///
    /// The root always carries exactly one TextMeshProUGUI. One
    /// UnityEngine.UI.Image is created only while the Done state needs it and the
    /// sprite is actually resolved, and destroyed when leaving Done. Countdown and
    /// Ready reserve zero icon space and own no Image. While a Done icon load is
    /// pending or has failed, the whole dependent Done presentation is suppressed
    /// by the caller; the caller keeps the readable Done business state and leaves
    /// the owned request in flight.
    ///
    /// Styling is fail-closed: if the native font/material cannot be read or a
    /// style value cannot be copied, the owned display is destroyed rather than
    /// left showing partial or substitute styling, and a later tick retries.
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

        // Neutral single-line probe used only to measure the native font's line
        // height; it is never displayed.
        private const string LineHeightProbe = "0";
        private const float LineHeightProbeWidth = 4096f;

        private bool fieldsResolved;
        private FieldInfo happinessLabelField;
        private FieldInfo wheelField;
        private FieldInfo leftUpGroupField;

        private HUD boundHud;
        private GameObject boundGroup;
        private TextMeshProUGUI boundStyleSource;
        private Material ownedMaterial;

        private GameObject displayObject;
        private TextMeshProUGUI label;
        private GameObject iconObject;
        private Image icon;

        private string appliedLanguage;
        private string appliedText;
        private float appliedAvailable = -1f;
        private SermonDisplayState appliedState = SermonDisplayState.Hidden;
        private Sprite appliedIcon;
        private bool styleDirty;

        private bool styleApplied;
        private TMP_FontAsset appliedFont;
        private Material appliedSharedMaterial;
        private float appliedFontSize;
        private FontStyles appliedFontStyle;
        private Color appliedColor;
        private float appliedOutlineWidth;
        private Color32 appliedOutlineColor;

        private bool reboundThisUpdate;

        internal void MarkStyleDirty() => styleDirty = true;

        private SermonHudResult Failed(string reason) =>
            new SermonHudResult(SermonHudStatus.Failed, reason, reboundThisUpdate);

        private SermonHudResult Pending() =>
            new SermonHudResult(SermonHudStatus.Pending, null, reboundThisUpdate);

        private SermonHudResult Healthy() =>
            new SermonHudResult(SermonHudStatus.Healthy, null, reboundThisUpdate);

        /// <summary>Hide without destroying; the caller's owned sprite handle is untouched.</summary>
        internal void Hide()
        {
            if (displayObject != null) displayObject.SetActive(false);
        }

        /// <summary>
        /// Show the given display state with its already-resolved text.
        /// <paramref name="iconSprite"/> non-null means Done may present its icon; a
        /// null sprite with state Done means the dependent Done presentation is
        /// suppressed (the caller keeps the request in flight).
        /// </summary>
        internal SermonHudResult Update(SermonDisplayState state, string text, Sprite iconSprite)
        {
            reboundThisUpdate = false;

            if (!EnsureFields())
                return Failed("native HUD members unavailable");

            HUD hud = TryGetHud();
            if (hud == null)
            {
                // No live HUD right now (loading, menu, or between rebuilds): a
                // normal pending condition, never a native resource fault. If a
                // display was bound before, the host was rebuilt, so the caller
                // releases the old sprite reference for the new binding.
                if (displayObject != null) reboundThisUpdate = true;
                DestroyOwnedDisplay();
                return Pending();
            }

            GameObject group = ReadMember<GameObject>(leftUpGroupField, hud);
            TextMeshProUGUI source = ReadMember<TextMeshProUGUI>(happinessLabelField, hud);
            UIHUDWheel wheel = ReadMember<UIHUDWheel>(wheelField, hud);

            // A normally inactive native group is not a failure; a missing or
            // destroyed member is. We never fall back to alternate names.
            if (group == null || source == null || wheel == null)
                return Failed("native HUD members unavailable");

            if (displayObject == null)
                ResetBindingRefs();
            else if (!ReferenceEquals(boundHud, hud)
                     || !ReferenceEquals(boundGroup, group)
                     || !ReferenceEquals(boundStyleSource, source))
            {
                // Native HUD or source instance was replaced: replace our display
                // too and report the rebind so the caller re-requests the sprite.
                DestroyOwnedDisplay();
                reboundThisUpdate = true;
            }

            if (displayObject == null && !TryCreateDisplay(hud, group, source))
                return Failed("display creation failed");

            string language = SermonReminderLocalization.CurrentLanguageId;
            if (!styleApplied || styleDirty || !string.Equals(appliedLanguage, language, StringComparison.Ordinal)
                || SourceStyleChanged(source))
            {
                if (!ApplyStyle(source, language))
                {
                    // Missing/unreadable style source: never keep a partial or
                    // substitute style. Destroy the display and retry later.
                    DestroyOwnedDisplay();
                    return Failed("native style source unavailable");
                }
            }

            RectTransform parentRect = group.transform as RectTransform;
            RectTransform wheelRect = wheel.transform as RectTransform;
            if (parentRect == null || wheelRect == null)
            {
                DestroyOwnedDisplay();
                return Failed("native rect transforms unavailable");
            }

            if (!TryGetCanvasLocalBounds(parentRect, out float canvasMinX, out float canvasMaxX))
            {
                // No resolvable canvas bounds: a resource failure, not a reason to
                // invent placeholder dimensions. Fail closed and retry later.
                DestroyOwnedDisplay();
                return Failed("native canvas bounds unavailable");
            }

            bool done = state == SermonDisplayState.Done;
            bool iconReady = done && iconSprite != null;

            float available = Mathf.Max(1f, (canvasMaxX - canvasMinX) - LocalGap * 2f);

            bool layoutCurrent = displayObject.activeSelf
                && appliedState == state
                && ReferenceEquals(appliedIcon, iconSprite)
                && Mathf.Approximately(appliedAvailable, available)
                && string.Equals(appliedText, text, StringComparison.Ordinal);

            if (!layoutCurrent)
            {
                if (iconReady)
                {
                    if (!LayoutWithIcon(text, iconSprite, available))
                        return Failed("native text metrics unavailable");
                }
                else
                {
                    LayoutWithoutIcon(state, text, available);
                }
            }

            PositionDisplay(wheelRect, parentRect, canvasMinX, canvasMaxX);
            displayObject.SetActive(true);
            return Healthy();
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

        /// <summary>Countdown/Ready: full available width, no icon and no reserved space.</summary>
        private void LayoutWithoutIcon(SermonDisplayState state, string text, float available)
        {
            DestroyOwnedIcon();

            var preferred = Measure(text, available);
            float height = Mathf.Max(preferred.y, 1f);
            float width = Mathf.Clamp(preferred.x, 1f, available);

            ApplyTextLayout(text, width, height);
            ApplyDisplaySize(width, height);
            RememberLayout(state, text, available, null);
        }

        /// <summary>
        /// Done: reserve the icon row space, measure the remaining text width,
        /// then size the icon from the actual native text line height so the
        /// combined display stays inside the real canvas.
        /// </summary>
        private bool LayoutWithIcon(string text, Sprite sprite, float available)
        {
            float lineHeight = MeasureLineHeight();
            if (lineHeight <= 0f)
            {
                DestroyOwnedDisplay();
                return false;
            }

            float iconHeight = lineHeight;
            float iconWidth = lineHeight * (IconAspectWidth / IconAspectHeight);
            float reserved = iconWidth + LocalGap;
            float remaining = Mathf.Max(1f, available - reserved);

            var preferred = Measure(text, remaining);
            float textHeight = Mathf.Max(preferred.y, 1f);
            float textWidth = Mathf.Clamp(preferred.x, 1f, remaining);

            float displayWidth = Mathf.Min(textWidth + reserved, available);
            float displayHeight = Mathf.Max(textHeight, iconHeight);
            float finalTextWidth = Mathf.Max(1f, displayWidth - reserved);

            ApplyTextLayout(text, finalTextWidth, textHeight);
            ApplyDisplaySize(displayWidth, displayHeight);

            if (iconObject == null && !TryCreateIcon())
                return false;

            if (!ApplyIconSpriteAndStyle(sprite))
            {
                DestroyOwnedIcon();
                return false;
            }

            var iconRect = (RectTransform)iconObject.transform;
            iconRect.anchoredPosition = new Vector2(finalTextWidth + LocalGap, 0f);
            iconRect.sizeDelta = new Vector2(iconWidth, iconHeight);

            RememberLayout(SermonDisplayState.Done, text, available, sprite);
            return true;
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
            try
            {
                root = new GameObject(DisplayObjectName, typeof(RectTransform));

                // Register for cleanup immediately: if parenting or component
                // creation fails, the object can never be orphaned.
                displayObject = root;

                var rootRect = (RectTransform)root.transform;
                rootRect.anchorMin = new Vector2(0f, 1f);
                rootRect.anchorMax = new Vector2(0f, 1f);
                rootRect.pivot = new Vector2(0.5f, 1f);

                root.transform.SetParent(group.transform, false);

                // One owned TMP child carries the text so the conditional Done
                // Image can keep sibling order before it. Destroying the root
                // disposes the child with it.
                var textGo = new GameObject(TextObjectName, typeof(RectTransform));
                textGo.transform.SetParent(root.transform, false);

                var text = textGo.AddComponent<TextMeshProUGUI>();
                text.raycastTarget = false;

                label = text;
                boundHud = hud;
                boundGroup = group;
                boundStyleSource = source;
                styleApplied = false;
                ResetLayoutCache();
                return true;
            }
            catch (Exception)
            {
                if (root != null) UnityEngine.Object.Destroy(root);
                displayObject = null;
                label = null;
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
                // Image sits under the native HUD and renders before the Done
                // text child.
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

                label.alignment = TextAlignmentOptions.TopLeft;
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
                // and icon size are stale. Invalidate them so the next Update
                // recomputes wrapping and the icon geometry.
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

        /// <summary>
        /// Measure the sentence directly from the font metrics. This does not depend
        /// on the label's active state, unlike a mesh update, so a normally
        /// inactive native group still yields correct dimensions.
        /// </summary>
        private Vector2 Measure(string text, float width)
        {
            return label.GetPreferredValues(text, Mathf.Max(1f, width), 0f);
        }

        /// <summary>
        /// The native font's single unwrapped line height, measured from the styled
        /// label's own font metrics. A single-line preferred-value measurement is
        /// independent of the displayed sentence (it is never the whole wrapped
        /// paragraph height) and scales with the applied native font size, so the
        /// icon follows native canvas scaling instead of a hardcoded pixel height.
        /// </summary>
        private float MeasureLineHeight()
        {
            if (label == null) return 0f;

            Vector2 measured = label.GetPreferredValues(LineHeightProbe, LineHeightProbeWidth, 0f);
            float lineHeight = measured.y;
            return lineHeight > 0f ? lineHeight : 0f;
        }

        private void ApplyTextLayout(string text, float width, float height)
        {
            if (!string.Equals(appliedText, text, StringComparison.Ordinal))
            {
                label.text = text;
                appliedText = text;
            }

            label.rectTransform.anchorMin = new Vector2(0f, 1f);
            label.rectTransform.anchorMax = new Vector2(0f, 1f);
            label.rectTransform.pivot = new Vector2(0f, 1f);
            label.rectTransform.anchoredPosition = Vector2.zero;
            label.rectTransform.sizeDelta = new Vector2(width, height);
        }

        private void ApplyDisplaySize(float width, float height)
        {
            if (displayObject != null)
            {
                var rect = (RectTransform)displayObject.transform;
                rect.anchorMin = new Vector2(0f, 1f);
                rect.anchorMax = new Vector2(0f, 1f);
                rect.pivot = new Vector2(0.5f, 1f);
                rect.sizeDelta = new Vector2(width, height);
            }
        }

        /// <summary>
        /// Anchor the combined display under the clock and clamp it inside the real
        /// canvas width. Anchors are (0,1); anchoredPosition is measured from the
        /// parent's top-left corner in parent-local units.
        /// </summary>
        private void PositionDisplay(RectTransform wheelRect, RectTransform parentRect, float canvasMinX, float canvasMaxX)
        {
            var corners = new Vector3[4];
            wheelRect.GetWorldCorners(corners);

            float minX = float.MaxValue;
            float maxX = float.MinValue;
            float minY = float.MaxValue;
            for (int i = 0; i < 4; i++)
            {
                Vector3 local = parentRect.InverseTransformPoint(corners[i]);
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

            float anchorLeftX = parentRect.rect.xMin;
            float anchorTopY = parentRect.rect.yMax;
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

            if (canvasRect == parentRect)
            {
                // The parent is the canvas itself: its own rect is already the
                // canvas space, so use it directly rather than 0..width.
                minX = parentRect.rect.xMin;
                maxX = parentRect.rect.xMax;
                return true;
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

            return true;
        }

        private void RememberLayout(SermonDisplayState state, string text, float available, Sprite iconSprite)
        {
            appliedState = state;
            appliedAvailable = available;
            appliedIcon = iconSprite;
        }

        private void DestroyOwnedIcon()
        {
            if (iconObject != null)
            {
                iconObject.SetActive(false);
                UnityEngine.Object.Destroy(iconObject);
            }

            iconObject = null;
            icon = null;
        }

        private void DestroyOwnedDisplay()
        {
            if (displayObject != null)
            {
                displayObject.SetActive(false);
                UnityEngine.Object.Destroy(displayObject);
            }

            if (ownedMaterial != null)
            {
                UnityEngine.Object.Destroy(ownedMaterial);
                ownedMaterial = null;
            }

            displayObject = null;
            label = null;
            iconObject = null;
            icon = null;
            appliedLanguage = null;
            ResetBindingRefs();
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
            styleApplied = false;
            ResetLayoutCache();
        }
    }
}
