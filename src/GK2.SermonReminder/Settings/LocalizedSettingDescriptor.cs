using System;
using System.Collections.Generic;
using BepInEx.Configuration;
using GK2.Framework;
using GK2.SermonReminder.Localization;

namespace GK2.SermonReminder.Settings
{
    /// <summary>
    /// Read-only presentation wrapper around one of the mod's own registered
    /// settings.
    ///
    /// Only the localized display fields are dynamic: <see cref="Section"/> resolves
    /// the approved live settings-group title, and <see cref="DisplayName"/> /
    /// <see cref="Description"/> resolve the supplied stable catalog keys against the
    /// current native language. Every structural member is forwarded to the inner
    /// setting, so the underlying config identity, unique key, value, default, type,
    /// kind, range, choices, order, read-only flag and reset semantics are preserved
    /// exactly.
    ///
    /// <see cref="ValueChanged"/> forwards subscriptions to the inner event on add /
    /// remove and never subscribes to it permanently, so this wrapper introduces no
    /// unmanaged subscription of its own.
    /// </summary>
    internal sealed class LocalizedSettingDescriptor : IGk2Setting
    {
        private readonly IGk2Setting inner;
        private readonly Func<string> groupTitle;
        private readonly string labelKey;
        private readonly string descriptionKey;

        internal LocalizedSettingDescriptor(
            IGk2Setting inner,
            Func<string> groupTitle,
            string labelKey,
            string descriptionKey)
        {
            this.inner = inner ?? throw new ArgumentNullException(nameof(inner));
            this.groupTitle = groupTitle;
            this.labelKey = labelKey;
            this.descriptionKey = descriptionKey;
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

        public string DisplayName => Resolve(labelKey);

        public string Description => Resolve(descriptionKey);

        /// <summary>
        /// Resolve one approved catalog key against the live language. Fails closed to
        /// an empty presentation on a missing key or a lookup fault rather than
        /// inventing or substituting copy.
        /// </summary>
        private static string Resolve(string key)
        {
            if (string.IsNullOrEmpty(key)) return string.Empty;
            try
            {
                return SermonReminderLocalization.Get(key) ?? string.Empty;
            }
            catch (Exception)
            {
                return string.Empty;
            }
        }

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

    /// <summary>Outcome of one owned-toggle registration transaction.</summary>
    internal readonly struct OwnToggleRegistration
    {
        internal ConfigEntry<bool> Entry { get; }
        internal bool Succeeded { get; }
        internal string Failure { get; }

        internal OwnToggleRegistration(ConfigEntry<bool> entry, bool succeeded, string failure)
        {
            Entry = entry;
            Succeeded = succeeded;
            Failure = failure;
        }
    }

    /// <summary>
    /// Owned settings registration shared by the mod's own toggles. The mutable list
    /// is proven before any registration, our own descriptor is identified by a proven
    /// pre-registration snapshot delta AND its exact requested section/key/bool
    /// identity, wrapped for localized presentation, and (if needed) rolled back
    /// strictly by reference so no other mod's setting is ever touched and no global
    /// patch or localization-manager mutation is used. An unexpected list shape, an
    /// ambiguous own descriptor, or a failed mutation is reported as a contained
    /// failure, never silently degraded and never replaced by an invented fallback
    /// registration.
    /// </summary>
    internal static class LocalizedSettingRegistration
    {
        /// <summary>
        /// Register one own toggle and localize its presentation in a single
        /// parameterized transaction. Returns a contained failure (with no usable
        /// entry) on any ambiguity so the owning feature fails closed.
        /// </summary>
        internal static OwnToggleRegistration RegisterOwnToggle(
            Gk2Settings settings,
            string section,
            string key,
            bool defaultValue,
            int order,
            string labelKey,
            string descriptionKey,
            Func<string> groupTitle)
        {
            try
            {
                // Validate the mutable list shape and readability BEFORE any registration.
                if (!TryGetMutableList(settings, out IList<IGk2Setting> list, out string listFailure))
                    return new OwnToggleRegistration(null, false, listFailure);

                if (!TrySnapshotItems(list, out HashSet<IGk2Setting> before, out string snapshotFailure))
                    return new OwnToggleRegistration(null, false, snapshotFailure);

                ConfigEntry<bool> entry = settings.AddToggle(
                    section, key, defaultValue, string.Empty, string.Empty, order);

                // Identify our exact new descriptor by proven reference delta AND the
                // expected stable identity, requiring one unambiguous candidate; never
                // fall back to an arbitrary item.
                IGk2Setting registered = FindOwnDescriptor(list, before, section, key);
                if (registered == null)
                {
                    // Cannot prove which entry is ours: leave the list untouched and
                    // fail closed rather than remove or change an unidentified item.
                    return new OwnToggleRegistration(null, false,
                        "registration produced no unambiguous own descriptor");
                }

                if (entry == null)
                {
                    RollBackOwnDescriptor(list, registered);
                    return new OwnToggleRegistration(null, false, "registration returned no config entry");
                }

                var localized = new LocalizedSettingDescriptor(registered, groupTitle, labelKey, descriptionKey);

                if (!TryReplaceOwnDescriptor(list, registered, localized, out string replaceFailure))
                {
                    // Fail closed: never keep an unlocalized descriptor usable. Roll back
                    // only our own just-added entry when that is safely possible.
                    RollBackOwnDescriptor(list, registered);
                    return new OwnToggleRegistration(null, false,
                        "localization swap failed (" + replaceFailure + ")");
                }

                return new OwnToggleRegistration(entry, true, null);
            }
            catch (Exception ex)
            {
                return new OwnToggleRegistration(null, false, ex.GetType().Name);
            }
        }

        /// <summary>
        /// Remove exactly our own just-added descriptor after a failed step, so no
        /// unlocalized setting remains registered. Never removes or changes another
        /// mod's entry; a failed rollback is reported, not forced.
        /// </summary>
        private static void RollBackOwnDescriptor(IList<IGk2Setting> list, IGk2Setting registered)
        {
            TryRemoveOwnDescriptor(list, registered, out _);
        }

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
        /// Capture the proven pre-registration set of descriptors so a later
        /// registration can be identified by an exact reference delta.
        /// </summary>
        internal static bool TrySnapshotItems(
            IList<IGk2Setting> list,
            out HashSet<IGk2Setting> snapshot,
            out string failure)
        {
            snapshot = null;
            failure = null;
            try
            {
                var captured = new HashSet<IGk2Setting>();
                for (int i = 0; i < list.Count; i++)
                {
                    IGk2Setting item = list[i];
                    if (item != null) captured.Add(item);
                }

                snapshot = captured;
                return true;
            }
            catch (Exception ex)
            {
                failure = "settings snapshot unreadable (" + ex.GetType().Name + ")";
                return false;
            }
        }

        /// <summary>
        /// Find the one descriptor a registration added: it must be absent from the
        /// proven pre-registration snapshot AND carry the expected stable identity
        /// (section / key / bool). Zero or multiple matches report null so the caller
        /// fails closed instead of selecting an arbitrary or pre-existing descriptor.
        /// </summary>
        internal static IGk2Setting FindOwnDescriptor(
            IList<IGk2Setting> list,
            HashSet<IGk2Setting> before,
            string section,
            string key)
        {
            IGk2Setting candidate = null;
            int matches = 0;
            for (int i = 0; i < list.Count; i++)
            {
                IGk2Setting item = list[i];
                if (item == null || before.Contains(item))
                    continue;

                string itemSection;
                string itemKey;
                Type valueType;
                try
                {
                    itemSection = item.Section;
                    itemKey = item.Key;
                    valueType = item.ValueType;
                }
                catch (Exception)
                {
                    continue;
                }

                if (!string.Equals(itemSection, section, StringComparison.Ordinal)) continue;
                if (!string.Equals(itemKey, key, StringComparison.Ordinal)) continue;
                if (valueType != typeof(bool)) continue;

                candidate = item;
                matches++;
            }

            return matches == 1 ? candidate : null;
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
