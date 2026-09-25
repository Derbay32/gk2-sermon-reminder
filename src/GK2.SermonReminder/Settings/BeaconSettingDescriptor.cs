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

        /// <summary>Stable opaque config identity, independent of the localized section.</summary>
        public string UniqueKey => inner.UniqueKey;

        /// <summary>
        /// Live approved settings-group title. The delegate already carries the
        /// established localization fallback chain; when no approved localized title
        /// is available this fails closed to an empty presentation rather than
        /// displaying the opaque config section or inventing a sentence.
        /// </summary>
        public string Section
        {
            get
            {
                if (groupTitle == null) return string.Empty;
                try
                {
                    return groupTitle() ?? string.Empty;
                }
                catch (Exception)
                {
                    return string.Empty;
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
    /// Small helpers over the mod's own framework settings list. The mutable list is
    /// proven before any registration, our own descriptor is swapped and (if needed)
    /// rolled back strictly by reference so no other mod's setting is ever touched and
    /// no global patch or localization-manager mutation is used. An unexpected list
    /// shape or a failed mutation is reported as a contained failure, never silently
    /// degraded and never replaced by an invented fallback registration.
    /// </summary>
    internal static class BeaconSettingLocalization
    {
        /// <summary>
        /// Resolve the framework settings list as a mutable <see cref="IList{T}"/>,
        /// validating the shape and readability before anything is registered.
        /// </summary>
        internal static bool TryGetMutableList(
            Gk2Settings settings,
            out IList<IGk2Setting> list,
            out string failure)
        {
            list = null;
            failure = null;
            try
            {
                if (settings == null)
                {
                    failure = "settings unavailable";
                    return false;
                }

                if (!(settings.Items is IList<IGk2Setting> mutable))
                {
                    failure = "unexpected framework settings list shape";
                    return false;
                }

                list = mutable;
                return true;
            }
            catch (Exception ex)
            {
                failure = "settings list unreadable (" + ex.GetType().Name + ")";
                return false;
            }
        }

        /// <summary>
        /// Replace our exact registered descriptor by reference. Fails when the list
        /// shape is missing or our descriptor is no longer present, so nothing else is
        /// ever mutated.
        /// </summary>
        internal static bool TryReplaceOwnDescriptor(
            IList<IGk2Setting> list,
            IGk2Setting registered,
            IGk2Setting replacement,
            out string failure)
        {
            failure = null;
            try
            {
                if (list == null || registered == null || replacement == null)
                {
                    failure = "missing settings list or descriptor";
                    return false;
                }

                int index = IndexOfReference(list, registered);
                if (index < 0)
                {
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

        /// <summary>
        /// Remove our exact registered descriptor by reference, used only to roll back
        /// our own just-added entry after a failed localization swap. Other entries are
        /// never removed or changed.
        /// </summary>
        internal static bool TryRemoveOwnDescriptor(
            IList<IGk2Setting> list,
            IGk2Setting registered,
            out string failure)
        {
            failure = null;
            try
            {
                if (list == null || registered == null)
                {
                    failure = "missing settings list or descriptor";
                    return false;
                }

                int index = IndexOfReference(list, registered);
                if (index < 0)
                {
                    failure = "registered descriptor not present in settings list";
                    return false;
                }

                list.RemoveAt(index);
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
