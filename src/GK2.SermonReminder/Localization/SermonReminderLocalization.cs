using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using LazyBearTechnology;
using Newtonsoft.Json;

namespace GK2.SermonReminder.Localization
{
    /// <summary>
    /// Embedded per-language catalog. Business code only ever references stable
    /// semantic keys; no final sentence is written in C#.
    /// </summary>
    internal static class SermonReminderLocalization
    {
        internal const string SermonReminderKey = "gksr.hud.sermonReminder";
        internal const string SermonDoneKey = "gksr.hud.sermonDone";
        internal const string CountdownOneKey = "gksr.hud.sermonCountdown.one";
        internal const string CountdownOtherKey = "gksr.hud.sermonCountdown.other";
        internal const string SettingsGroupTitleKey = "gksr.settings.group.title";
        internal const string SettingsBeaconLabelKey = "gksr.settings.beacon.label";
        internal const string SettingsBeaconDescriptionKey = "gksr.settings.beacon.description";
        internal const string SettingsPopupLabelKey = "gksr.settings.popup.label";
        internal const string SettingsPopupDescriptionKey = "gksr.settings.popup.description";

        // GKSA-14 approved fault-notice copy. The status keys are the native notice
        // text (referenced by the notice core); the diagnostics keys are the separate
        // user-facing explanations logged alongside the real technical detail.
        internal const string StatusReminderUnavailableKey = "gksr.status.reminderUnavailable";
        internal const string StatusHudUnavailableKey = "gksr.status.hudUnavailable";
        internal const string DiagnosticsReminderUnavailableKey = "gksr.diagnostics.reminderUnavailable";
        internal const string DiagnosticsHudUnavailableKey = "gksr.diagnostics.hudUnavailable";

        private const string English = "en";
        private const string Chinese = "zh_cn";

        private const string EnglishResource = "gk2.sermonreminder.localization.en.json";
        private const string ChineseResource = "gk2.sermonreminder.localization.zh_cn.json";

        private static readonly Dictionary<string, Dictionary<string, string>> Catalog = BuildCatalog();

        /// <summary>
        /// Raw game language mapped to a supported catalog id. zh_cn is Chinese;
        /// every other raw id (including en and zh_cht) resolves to English.
        /// </summary>
        internal static string CurrentLanguageId
        {
            get
            {
                string raw = null;
                try { raw = LLBase.CurrentLang; }
                catch (Exception) { raw = null; }

                return string.Equals(raw, Chinese, StringComparison.OrdinalIgnoreCase) ? Chinese : English;
            }
        }

        internal static string Get(string key)
        {
            if (string.IsNullOrEmpty(key)) return string.Empty;

            string language = CurrentLanguageId;
            if (TryGet(language, key, out string value)) return value;
            if (TryGet(English, key, out value)) return value;
            return string.Empty;
        }

        private static bool TryGet(string language, string key, out string value)
        {
            value = null;
            if (Catalog.TryGetValue(language, out Dictionary<string, string> map)
                && map.TryGetValue(key, out string found)
                && !string.IsNullOrEmpty(found))
            {
                value = found;
                return true;
            }
            return false;
        }

        private static Dictionary<string, Dictionary<string, string>> BuildCatalog()
        {
            var catalog = new Dictionary<string, Dictionary<string, string>>(StringComparer.Ordinal)
            {
                [English] = Parse(ReadEmbedded(EnglishResource)),
                [Chinese] = Parse(ReadEmbedded(ChineseResource))
            };
            return catalog;
        }

        private static Dictionary<string, string> Parse(string json)
        {
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            if (string.IsNullOrEmpty(json)) return result;

            try
            {
                Dictionary<string, string> parsed = JsonConvert.DeserializeObject<Dictionary<string, string>>(json);
                if (parsed != null)
                {
                    foreach (KeyValuePair<string, string> pair in parsed)
                    {
                        if (!string.IsNullOrEmpty(pair.Key))
                            result[pair.Key] = pair.Value ?? string.Empty;
                    }
                }
            }
            catch (JsonException)
            {
            }

            return result;
        }

        private static string ReadEmbedded(string logicalName)
        {
            Assembly assembly = typeof(SermonReminderLocalization).Assembly;
            using (Stream stream = assembly.GetManifestResourceStream(logicalName))
            {
                if (stream == null) return null;
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                    return reader.ReadToEnd();
            }
        }
    }
}
