using System;
using System.Reflection;
using GK2.SermonReminder.Localization;
using HarmonyLib;
using LazyBearTechnology;
using TMPro;
using UnityEngine;

namespace GK2.SermonReminder.Hud
{
    /// <summary>
    /// Owns exactly one mod-created TextMeshProUGUI placed beneath the native
    /// clock. The label is parented under the native left-up group so it
    /// inherits the original HUD's visibility and layering; it never creates its
    /// own Canvas or sorting order.
    ///
    /// Styling is fail-closed: if the native font/material cannot be read or a
    /// style value cannot be copied, the owned display is destroyed rather than
    /// left showing partial or substitute styling, and a later tick retries.
    /// </summary>
    internal sealed class CountdownHudAdapter
    {
        private const string CountdownObjectName = "GK2SermonReminder.Countdown";
        private const string HappinessLabelFieldName = "happinessLabel";
        private const string WheelFieldName = "wheel";
        private const string LeftUpGroupFieldName = "leftUpGroup";

        // Small local UI-unit gap; parent-local units scale with the native canvas.
        private const float LocalGap = 6f;

        private bool fieldsResolved;
        private FieldInfo happinessLabelField;
        private FieldInfo wheelField;
        private FieldInfo leftUpGroupField;

        private HUD boundHud;
        private GameObject boundGroup;
        private TextMeshProUGUI boundStyleSource;
        private Material ownedMaterial;
        private GameObject labelObject;
        private TextMeshProUGUI label;

        private string appliedLanguage;
        private string appliedText;
        private float appliedWidth = -1f;
        private bool styleDirty;

        private bool styleApplied;
        private TMP_FontAsset appliedFont;
        private Material appliedSharedMaterial;
        private float appliedFontSize;
        private FontStyles appliedFontStyle;
        private Color appliedColor;
        private float appliedOutlineWidth;
        private Color32 appliedOutlineColor;

        internal void MarkStyleDirty() => styleDirty = true;

        /// <summary>Hide without destroying; used when a save is active but hidden.</summary>
        internal void Hide()
        {
            if (labelObject != null) labelObject.SetActive(false);
        }

        /// <summary>
        /// Show the given text beneath the native clock. Returns false when the
        /// native HUD, a required member, or the native style source is
        /// unavailable or unreadable (the owned display is destroyed in that
        /// case), so the caller can emit one bounded diagnostic.
        /// </summary>
        internal bool Update(string text)
        {
            if (!EnsureFields())
            {
                Teardown();
                return false;
            }

            HUD hud = TryGetHud();
            if (hud == null)
            {
                Teardown();
                return false;
            }

            GameObject group = ReadMember<GameObject>(leftUpGroupField, hud);
            TextMeshProUGUI source = ReadMember<TextMeshProUGUI>(happinessLabelField, hud);
            UIHUDWheel wheel = ReadMember<UIHUDWheel>(wheelField, hud);

            // A normally inactive native group is not a failure; a missing or
            // destroyed member is. We never fall back to alternate names.
            if (group == null || source == null || wheel == null)
            {
                Teardown();
                return false;
            }

            if (labelObject == null)
                ResetBindingRefs();
            else if (!ReferenceEquals(boundHud, hud)
                     || !ReferenceEquals(boundGroup, group)
                     || !ReferenceEquals(boundStyleSource, source))
            {
                // Native HUD or source instance was replaced: replace our label too.
                Teardown();
            }

            if (labelObject == null && !TryCreateLabel(hud, group, source))
            {
                Teardown();
                return false;
            }

            string language = SermonReminderLocalization.CurrentLanguageId;
            if (!styleApplied || styleDirty || !string.Equals(appliedLanguage, language, StringComparison.Ordinal)
                || SourceStyleChanged(source))
            {
                if (!ApplyStyle(source, language))
                {
                    // Missing/unreadable style source: never keep a partial or
                    // substitute style. Destroy the display and retry later.
                    Teardown();
                    return false;
                }
            }

            labelObject.SetActive(true);

            RectTransform parentRect = group.transform as RectTransform;
            RectTransform wheelRect = wheel.transform as RectTransform;
            if (parentRect == null || wheelRect == null)
            {
                Teardown();
                return false;
            }

            if (!TryGetCanvasLocalBounds(parentRect, out float canvasMinX, out float canvasMaxX))
            {
                // No resolvable canvas bounds: a resource failure, not a reason to
                // invent placeholder dimensions. Fail closed and retry later.
                Teardown();
                return false;
            }

            float available = Mathf.Max(1f, (canvasMaxX - canvasMinX) - LocalGap * 2f);
            if (!string.Equals(appliedText, text, StringComparison.Ordinal)
                || !Mathf.Approximately(appliedWidth, available))
            {
                ApplyText(text, available);
            }

            Reposition(wheelRect, parentRect, canvasMinX, canvasMaxX);
            return true;
        }

        internal void Teardown()
        {
            if (labelObject != null)
            {
                labelObject.SetActive(false);
                UnityEngine.Object.Destroy(labelObject);
            }

            if (ownedMaterial != null)
            {
                UnityEngine.Object.Destroy(ownedMaterial);
                ownedMaterial = null;
            }

            labelObject = null;
            label = null;
            appliedLanguage = null;
            styleDirty = false;
            ResetBindingRefs();
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

        private bool TryCreateLabel(HUD hud, GameObject group, TextMeshProUGUI source)
        {
            GameObject go = null;
            try
            {
                go = new GameObject(CountdownObjectName, typeof(RectTransform));

                // Register for cleanup immediately: if parenting or component
                // creation fails, the object can never be orphaned.
                labelObject = go;

                go.transform.SetParent(group.transform, false);

                var text = go.AddComponent<TextMeshProUGUI>();
                text.raycastTarget = false;

                label = text;
                boundHud = hud;
                boundGroup = group;
                boundStyleSource = source;
                styleApplied = false;
                appliedText = null;
                appliedWidth = -1f;
                return true;
            }
            catch (Exception)
            {
                if (go != null) UnityEngine.Object.Destroy(go);
                labelObject = null;
                label = null;
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

                label.alignment = TextAlignmentOptions.Top;
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
                // is stale. Invalidate it so the next Update recomputes the
                // preferred size and wrapping for the current sentence.
                appliedText = null;
                appliedWidth = -1f;
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

        private void ApplyText(string text, float available)
        {
            label.text = text;

            // Measure the wrapped size directly from the font metrics. This does
            // not depend on the label's active state, unlike a mesh update, so a
            // normally inactive native group still yields correct dimensions.
            Vector2 measured = label.GetPreferredValues(text, available, 0f);
            float width = Mathf.Clamp(measured.x, 1f, available);
            float height = Mathf.Max(measured.y, 1f);
            label.rectTransform.sizeDelta = new Vector2(width, height);

            appliedText = text;
            appliedWidth = available;
        }

        private void Reposition(RectTransform wheelRect, RectTransform parentRect, float canvasMinX, float canvasMaxX)
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

            RectTransform rect = label.rectTransform;
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);

            float half = rect.sizeDelta.x * 0.5f;
            float lowerBound = canvasMinX + half + LocalGap;
            float upperBound = canvasMaxX - half - LocalGap;
            float desiredX = (minX + maxX) * 0.5f;
            float pivotLocalX = lowerBound <= upperBound
                ? Mathf.Clamp(desiredX, lowerBound, upperBound)
                : (canvasMinX + canvasMaxX) * 0.5f;
            float pivotLocalY = minY - LocalGap;

            // Anchors are (0,1); anchoredPosition is measured from the parent's
            // top-left corner in parent-local units.
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

        private void ResetBindingRefs()
        {
            boundHud = null;
            boundGroup = null;
            boundStyleSource = null;
            styleApplied = false;
            appliedText = null;
            appliedWidth = -1f;
        }
    }
}
