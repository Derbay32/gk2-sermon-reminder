using System;
using System.Collections.Generic;
using GK2.Framework;
using GK2.SermonReminder.Localization;

namespace GK2.SermonReminder.Settings
{
    /// <summary>
    /// Read-only presentation wrapper around the mod's own registered setting.
    ///
    /// Only the localized display fields are dynamic: <see cref="Section"/> resolves
    /// the approved live settings-group title, and <see cref="DisplayName"/> /
    /// <see cref="Description"/> resolve the beacon catalog keys against the current
    /// native language. Every structural member is forwarded to the inner setting, so
    /// the underlying config identity, unique key, value, default, type, kind, range,
    /// choices, order, read-only flag and reset semantics are preserved exactly.
    ///
    /// <see cref="ValueChanged"/> forwards subscriptions to the inner event on add /
    /// remove and never subscribes to it permanently, so this wrapper introduces no
    /// unmanaged subscription of its own.
    /// </summary>
    internal sealed class BeaconSettingDescriptor : IGk2Setting
    {
        private readonly IGk2Setting inner;
        private readonly Func<string> groupTitle;

        internal BeaconSettingDescriptor(IGk2Setting inner, Func<string> groupTitle)
        {
            this.inner = inner ?? throw new ArgumentNullException(nameof(inner));
            this.groupTitle = groupTitle;
        }

        /// <summary>The wrapped framework descriptor whose identity is preserved.</summary>
        internal IGk2Setting Inner => inner;

        /// <summary>Stable opaque config identity, independent of the localized section.</summary>
        public string UniqueKey => inner.UniqueKey;

        /// <summary>Live approved settings-group title, falling back to the inner section.</summary>
        public string Section
        {
            get
            {
                if (groupTitle == null) return inner.Section;
                try
                {
                    string title = groupTitle();
                    return string.IsNullOrEmpty(title) ? inner.Section : title;
                }
                catch (Exception)
                {
                    return inner.Section;
                }
            }
        }

        public string Key => inner.Key;

        public string DisplayName =>
            SermonReminderLocalization.Get(SermonReminderLocalization.SettingsBeaconLabelKey);

        public string Description =>
            SermonReminderLocalization.Get(SermonReminderLocalization.SettingsBeaconDescriptionKey);

        public SettingKind Kind => inner.Kind;
        public Type ValueType => inner.ValueType;

        public object Value
        {
            get => inner.Value;
            set => inner.Value = value;
        }

        public object DefaultValue => inner.DefaultValue;
        public object Minimum => inner.Minimum;
        public object Maximum => inner.Maximum;
        public double Step => inner.Step;
        public IReadOnlyList<object> Choices => inner.Choices;
        public bool IsReadOnly => inner.IsReadOnly;
        public int Order => inner.Order;

        public event Action<IGk2Setting> ValueChanged
        {
            add => inner.ValueChanged += value;
            remove => inner.ValueChanged -= value;
        }

        public void ResetToDefault() => inner.ResetToDefault();
    }

    /// <summary>
    /// Replaces exactly the mod's own just-registered descriptor inside the framework
    /// settings list with the localized wrapper. The list is read through a checked
    /// <see cref="IList{T}"/> view and only the descriptor that matches our own entry
    /// identity is swapped by reference, so no other mod's setting is touched and no
    /// global patch or localization-manager mutation is used. An unexpected list shape
    /// or a failed swap is reported as a contained failure, never silently retried.
    /// </summary>
    internal static class BeaconSettingLocalization
    {
        internal static bool TryReplaceOwnDescriptor(
            Gk2Settings settings,
            IGk2Setting registered,
            IGk2Setting replacement,
            out string failure)
        {
            failure = null;
            try
            {
                if (settings == null || registered == null || replacement == null)
                {
                    failure = "missing settings list or descriptor";
                    return false;
                }

                if (!(settings.Items is IList<IGk2Setting> list))
                {
                    // Unexpected framework list shape: report and keep the original
                    // descriptor rather than inventing a fallback registration.
                    failure = "unexpected framework settings list shape";
                    return false;
                }

                int index = IndexOfReference(list, registered);
                if (index < 0)
                {
                    // Unexpected shape: the just-registered descriptor is not in the
                    // list. Report a contained failure instead of inventing a fallback
                    // registration by adding a new item.
                    failure = "registered descriptor not present in settings list";
                    return false;
                }

                list[index] = replacement;
                return true;
            }
            catch (Exception ex)
            {
                failure = ex.GetType().Name;
                return false;
            }
        }

        private static int IndexOfReference(IList<IGk2Setting> list, IGk2Setting target)
        {
            for (int i = 0; i < list.Count; i++)
            {
                if (ReferenceEquals(list[i], target))
                    return i;
            }

            return -1;
        }
    }
}
