using System;
using System.Collections.Generic;
using System.Globalization;
using BepInEx;
using BepInEx.Configuration;
using GK2.Framework;
using GK2.SermonReminder.Beacon;
using GK2.SermonReminder.Hud;
using GK2.SermonReminder.Localization;
using GK2.SermonReminder.Notifications;
using GK2.SermonReminder.Settings;
using GK2.SermonReminder.State;
using HarmonyLib;
using UnityEngine;

namespace GK2.SermonReminder
{
    [BepInPlugin(PluginGuid, PluginName, PluginVersion)]
    [BepInDependency(FrameworkPlugin.PluginGuid, "0.1.9")]
    public sealed class SermonReminderPlugin : BaseUnityPlugin
    {
        public const string PluginGuid = "com.derbay32.gk2.sermonreminder";
        public const string PluginName = "GK2 Sermon Reminder";
        public const string PluginVersion = "0.1.0";

        private SermonReminderMod mod;

        private void Awake()
        {
            // Persist each configuration change immediately. The framework shares one
            // ConfigFile per plugin, so this is set before any mod registration binds
            // its settings.
            Config.SaveOnConfigSet = true;

            mod = new SermonReminderMod();
            FrameworkApi.RegisterMod(mod, Config);
        }

        private void LateUpdate() => mod?.Tick();

        private void OnDestroy() => mod?.Shutdown();
    }

    /// <summary>
    /// Owns the sermon-corner lifecycle: readiness, a fresh per-tick state read,
    /// the single mod-owned display, and the owned native icon request.
    /// </summary>
    internal sealed class SermonReminderMod : Gk2ModBase
    {
        internal static SermonReminderMod Active { get; private set; }

        // Stable opaque config identity and default for the one beacon toggle.
        private const string BeaconSection = "Beacon";
        private const string BeaconKey = "EnabledChurchBeacon";

        private readonly IReadOnlyList<Gk2ModDependency> dependencies;
        private readonly SermonStateReader reader = new SermonStateReader();
        private readonly SermonHudAdapter hud = new SermonHudAdapter();
        private readonly NativeCheckSprite checkSprite = new NativeCheckSprite();
        private readonly ChurchBeacon beacon = new ChurchBeacon();

        // The single accepted two-category fault-notice module, created once at
        // registration with the registration logger and reused for every tick.
        private SermonFaultNotices notices;

        private Gk2ModMetadata metadata;
        private string metadataLanguage;

        private Gk2ModLogger log;
        private Harmony harmony;
        private bool subscribed;
        private bool enabled;

        private bool ready;
        private GameSave activeSave;

        // The mod's own registered beacon toggle, captured during registration as the
        // stable ConfigEntry value source. Null until registration AND own-descriptor
        // localization both succeed, which fails the beacon closed (disabled) rather
        // than falling back to an unlocalized descriptor or a silent default-on.
        private ConfigEntry<bool> beaconEntry;

        // Snapshot of the persisted beacon toggle for the current load. Established
        // exactly once on actual load readiness and immutable for that load; runtime
        // changes only affect the next load.
        private bool beaconEnabledForLoad;

        // Independently deduplicated diagnostic channels: state read, corner
        // rendering (including the Done icon), the sprite handle and the beacon. Each
        // holds its own last-logged episode so a healthy channel never clears or
        // toggles another, and each is cleared only when that channel's own work
        // actually completes or at a true lifecycle / eligibility boundary.
        private string lastStateDiagnostic;
        private string lastCornerDiagnostic;
        private string lastIconDiagnostic;
        private string lastBeaconDiagnostic;
        private SermonDisplayState loggedDisplay;
        private bool hasLoggedDisplay;

        internal SermonReminderMod()
        {
            dependencies = new[]
            {
                new Gk2ModDependency(FrameworkPlugin.PluginGuid, "0.1.9")
            };

            // Seed a non-null metadata so registration can never fail; the
            // display name is refreshed against the live native language below.
            metadata = BuildMetadata();
            metadataLanguage = SermonReminderLocalization.CurrentLanguageId;
            beaconEnabledForLoad = false;
        }

        /// <summary>
        /// The framework display name is the localized settings-group title, kept
        /// in sync with the current native language on every read. The description
        /// stays empty rather than inventing copy.
        /// </summary>
        public override Gk2ModMetadata Metadata
        {
            get
            {
                string language = SermonReminderLocalization.CurrentLanguageId;
                if (!string.Equals(metadataLanguage, language, StringComparison.Ordinal))
                {
                    Gk2ModMetadata rebuilt = TryBuildMetadata();
                    if (rebuilt != null)
                    {
                        metadata = rebuilt;
                        metadataLanguage = language;
                    }
                }

                return metadata;
            }
        }

        public override IReadOnlyList<Gk2ModDependency> Dependencies => dependencies;

        private static Gk2ModMetadata BuildMetadata()
        {
            // Fall back to the stable English title only if the embedded resource
            // is unavailable, so the framework never shows the raw mod id.
            string displayName = SermonReminderLocalization.Get(SermonReminderLocalization.SettingsGroupTitleKey);
            if (string.IsNullOrEmpty(displayName)) displayName = SermonReminderPlugin.PluginName;

            return new Gk2ModMetadata(
                SermonReminderPlugin.PluginGuid,
                displayName,
                "derbay32",
                SermonReminderPlugin.PluginVersion,
                string.Empty,
                supportsRuntimeToggle: false,
                requiresKnownBuild: false,
                frameworkManagesEnabledState: false);
        }

        private static Gk2ModMetadata TryBuildMetadata()
        {
            try
            {
                return BuildMetadata();
            }
            catch (Exception)
            {
                // Keep the last good metadata rather than faulting a menu read.
                return null;
            }
        }

        public override void OnRegister(Gk2ModContext context)
        {
            try
            {
                log = context.Log;
                SafeInfo("GKSR_REGISTERED: version=" + SermonReminderPlugin.PluginVersion);
            }
            catch (Exception)
            {
                // A registration failure would fault the mod; keep registration safe.
            }

            // Build the notice module exactly once. A null logger is tolerated: the
            // module contains every log call, so an unavailable logger never faults.
            try { notices = new SermonFaultNotices(log); }
            catch (Exception) { notices = null; }

            RegisterBeaconSetting(context);
        }

        /// <summary>
        /// Register the one beacon toggle and localize its presentation. A failure is
        /// contained: the beacon then fails closed (disabled) for every load instead of
        /// silently defaulting on, while the corner presentation keeps working.
        /// </summary>
        private void RegisterBeaconSetting(Gk2ModContext context)
        {
            // Fail closed until registration and own-descriptor wrapping both succeed.
            beaconEntry = null;

            try
            {
                Gk2Settings settings = context?.Settings;

                // Validate the mutable list shape and readability BEFORE any registration.
                if (!BeaconSettingLocalization.TryGetMutableList(settings, out IList<IGk2Setting> list, out string listFailure))
                {
                    SafeLogError("GKSR_BEACON_SETTING_FAILED: " + listFailure);
                    return;
                }

                if (!TrySnapshotItems(list, out HashSet<IGk2Setting> before, out string snapshotFailure))
                {
                    SafeLogError("GKSR_BEACON_SETTING_FAILED: " + snapshotFailure);
                    return;
                }

                ConfigEntry<bool> entry = settings.AddToggle(
                    BeaconSection, BeaconKey, true, string.Empty, string.Empty, 0);

                // Identify our exact new descriptor by proven reference delta AND the
                // expected stable identity, requiring one unambiguous candidate; never
                // fall back to an arbitrary item.
                IGk2Setting registered = FindOwnDescriptor(list, before);
                if (registered == null)
                {
                    // Cannot prove which entry is ours: leave the list untouched and
                    // fail closed rather than remove or change an unidentified item.
                    SafeLogError("GKSR_BEACON_SETTING_FAILED: registration produced no unambiguous own descriptor");
                    return;
                }

                if (entry == null)
                {
                    RollBackOwnDescriptor(list, registered, "registration returned no config entry");
                    return;
                }

                var localized = new BeaconSettingDescriptor(
                    registered,
                    () => SermonReminderLocalization.Get(SermonReminderLocalization.SettingsGroupTitleKey));

                if (!BeaconSettingLocalization.TryReplaceOwnDescriptor(list, registered, localized, out string replaceFailure))
                {
                    // Fail closed: never keep an unlocalized descriptor usable. Roll back
                    // only our own just-added entry when that is safely possible.
                    RollBackOwnDescriptor(list, registered, "localization swap failed (" + replaceFailure + ")");
                    return;
                }

                // Only a fully registered and localized toggle becomes the value source.
                beaconEntry = entry;
            }
            catch (Exception ex)
            {
                beaconEntry = null;
                SafeLogError("GKSR_BEACON_SETTING_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Remove exactly our own just-added descriptor after a failed localization
        /// step, so no unlocalized beacon setting remains registered. Never removes or
        /// changes another mod's entry; a failed rollback is reported, not forced.
        /// </summary>
        private void RollBackOwnDescriptor(IList<IGk2Setting> list, IGk2Setting registered, string reason)
        {
            if (BeaconSettingLocalization.TryRemoveOwnDescriptor(list, registered, out string rollbackFailure))
            {
                SafeLogError("GKSR_BEACON_SETTING_FAILED: " + reason + "; own descriptor rolled back");
            }
            else
            {
                SafeLogError("GKSR_BEACON_SETTING_FAILED: " + reason
                    + "; rollback failed (" + rollbackFailure + ")");
            }
        }

        private static bool TrySnapshotItems(
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
        /// Find the one descriptor this registration added: it must be absent from the
        /// proven pre-registration snapshot AND carry the expected stable identity
        /// (Beacon / EnabledChurchBeacon / bool). Zero or multiple matches report null
        /// so the caller fails closed instead of selecting an arbitrary or pre-existing
        /// descriptor.
        /// </summary>
        private static IGk2Setting FindOwnDescriptor(IList<IGk2Setting> list, HashSet<IGk2Setting> before)
        {
            IGk2Setting candidate = null;
            int matches = 0;
            for (int i = 0; i < list.Count; i++)
            {
                IGk2Setting item = list[i];
                if (item == null || before.Contains(item))
                    continue;

                string section;
                string key;
                Type valueType;
                try
                {
                    section = item.Section;
                    key = item.Key;
                    valueType = item.ValueType;
                }
                catch (Exception)
                {
                    continue;
                }

                if (!string.Equals(section, BeaconSection, StringComparison.Ordinal)) continue;
                if (!string.Equals(key, BeaconKey, StringComparison.Ordinal)) continue;
                if (valueType != typeof(bool)) continue;

                candidate = item;
                matches++;
            }

            return matches == 1 ? candidate : null;
        }

        public override void OnEnable()
        {
            enabled = true;
            Active = this;

            try
            {
                harmony = new Harmony(SermonReminderPlugin.PluginGuid);
                harmony.PatchAll(typeof(MainGameLifecyclePatches));
            }
            catch (Exception ex)
            {
                SafeLogError("GKSR_PATCH_FAILED: " + ex.GetType().Name);
            }

            try
            {
                SaveSystem.OnSaveLoadingStarted += HandleNativeLoadStarting;
                GameSettings.OnLanguageChanged += HandleLanguageChanged;
                subscribed = true;
            }
            catch (Exception ex)
            {
                SafeLogError("GKSR_SUBSCRIBE_FAILED: " + ex.GetType().Name);
            }

            SafeInfo("GKSR_ENABLED");
        }

        public override void OnDisable()
        {
            enabled = false;
            SafeCleanup();
            SafeInfo("GKSR_DISABLED");
        }

        public override void OnGameStarted()
        {
            // Genuine readiness boundary. A duplicate callback without an intervening
            // native load/menu boundary must neither replenish the notice budgets nor
            // resnapshot the beacon setting, so only a real transition opens a load.
            if (ready)
                return;

            try
            {
                // Snapshot the beacon toggle once for this load BEFORE the save read, so
                // a missing/throwing initial save can never change the setting.
                beaconEnabledForLoad = ReadBeaconEnabledSnapshot();

                // Open exactly one notice load period for this ready period, even when
                // the initial save read is missing or throws: a prior successful state
                // read is not required before a fault can be reported.
                try { notices?.BeginLoad(); }
                catch (Exception) { }

                ready = true;
                activeSave = null;
                ResetDiagnostics();

                // Best-effort initial capture; a null result leaves the identity pending
                // and a later readable save is adopted once for this same ready period.
                GameSave save = TryGetCurrentSave();
                if (save != null)
                    activeSave = save;

                SafeInfo("GKSR_READY");
            }
            catch (Exception ex)
            {
                // The boundary was genuine, so the load stays open; report the failure
                // without discarding readiness or the opened notice period.
                SafeWarn("GKSR_READY_FAILED: " + ex.GetType().Name);
            }
        }

        public override void OnReturnedToMainMenu() => HandleReturnedToMenu();

        /// <summary>
        /// Called by early prefixes and SaveSystem.OnSaveLoadingStarted before a
        /// new save becomes authoritative. Contained so a Harmony prefix can never
        /// throw into the native method.
        /// </summary>
        internal void HandleNativeLoadStarting() => HandleLifecycleEnd();

        internal void HandleReturnedToMenu() => HandleLifecycleEnd();

        /// <summary>
        /// A genuine lifecycle boundary (native load start, menu return, disable or
        /// shutdown). Closes the notice load period so no stale notice can show, then
        /// drops readiness. Duplicate calls are harmless: only a real OnGameStarted
        /// transition opens a load and replenishes budgets.
        /// </summary>
        private void HandleLifecycleEnd()
        {
            try { notices?.EndLoad(); }
            catch (Exception) { }
            ResetReadinessSafely();
        }

        /// <summary>
        /// Read the persisted beacon toggle once for this load. A missing registration
        /// or a failed read is contained and fails the beacon closed (disabled) for the
        /// load, never a silent default-on.
        /// </summary>
        private bool ReadBeaconEnabledSnapshot()
        {
            try
            {
                ConfigEntry<bool> entry = beaconEntry;
                if (entry == null)
                {
                    SafeLogError("GKSR_BEACON_SETTING_FAILED: toggle unavailable; beacon disabled for this load");
                    return false;
                }

                // The successfully registered ConfigEntry is the stable value source.
                return entry.Value;
            }
            catch (Exception ex)
            {
                SafeLogError("GKSR_BEACON_SETTING_FAILED: read failed (" + ex.GetType().Name
                    + "); beacon disabled for this load");
                return false;
            }
        }

        internal void Tick()
        {
            // Fresh flags for THIS tick; no stale classification can survive.
            bool stateReadFailed = false;
            bool cornerHudFailed = false;
            bool stateReady = false;

            try
            {
                if (!enabled)
                {
                    // A disabled entry never starts a load or shows a notice. It still
                    // advances retained notice cleanup through the single finally call
                    // below, which cannot show because the notice load period is closed.
                    return;
                }

                if (!TickState(out stateReadFailed, out SermonStateSnapshot snapshot))
                    return;

                stateReady = true;
                TickCorner(snapshot, ref cornerHudFailed);
            }
            catch (Exception ex)
            {
                // Defensive last resort. The state and corner stages already contain their
                // own native failures; if the state read had succeeded the escape is a
                // corner failure and the healthy eligible beacon is preserved, otherwise
                // it is a state-read failure. Never blanket-classify every escape as state.
                if (stateReady)
                {
                    try { hud.Teardown(); }
                    catch (Exception) { }
                    ResetEligibility();
                    LogCornerDiagnostic("corner-escape", "GKSR_HUD_FAILED: unexpected corner escape", ex);
                    cornerHudFailed = true;
                }
                else
                {
                    SafeResetBeacon();
                    try { hud.Teardown(); }
                    catch (Exception) { }
                    ResetEligibility();
                    LogStateDiagnostic("state-escape", "GKSR_STATE_UNREADABLE: unexpected state escape", ex);
                    stateReadFailed = true;
                }
            }
            finally
            {
                // Exactly one contained flag delivery per tick for every path, so a
                // buffered failure is never discarded by a scattered flush.
                try { notices?.Update(stateReadFailed, cornerHudFailed); }
                catch (Exception) { }
            }
        }

        /// <summary>
        /// Release only the owned beacon resources, leaving the beacon diagnostic
        /// episode untouched so a persisting identical failure is not re-logged.
        /// </summary>
        private void ResetBeaconResources()
        {
            try { beacon.Reset(); }
            catch (Exception) { }
        }

        /// <summary>
        /// Release the owned beacon resources and end its diagnostic episode. Called only
        /// at true eligibility / load-reset boundaries, never from the defensive per-tick
        /// path, so a prior failure is preserved until a real boundary.
        /// </summary>
        private void SafeResetBeacon()
        {
            ResetBeaconResources();
            ClearBeaconDiagnostic();
        }

        /// <summary>
        /// State stage: resolve the current save, preserve the load identity, and read a
        /// fresh snapshot. Returns false when the tick must stop without a corner stage
        /// (not ready or a state-read failure); the two flags are left meaningful for the
        /// caller's single delivery. State reads fail as state-read and suppress only
        /// state-dependent displays, never the healthy beacon.
        /// </summary>
        private bool TickState(out bool stateReadFailed, out SermonStateSnapshot snapshot)
        {
            stateReadFailed = false;
            snapshot = default;

            if (!ready)
            {
                // Not-ready tick: never a state fault. Retained notice cleanup is advanced
                // by the caller's single delivery with both flags false.
                SafeResetBeacon();
                hud.Teardown();
                ResetEligibility();
                return false;
            }

            GameSave current;
            try
            {
                current = TryGetCurrentSave();
            }
            catch (Exception ex)
            {
                return StateFailure("save-read", "current save read failed", ex, out stateReadFailed, out snapshot);
            }

            if (activeSave == null && current != null)
            {
                // The initial capture during readiness failed; adopt the first
                // subsequently readable non-null save once for this same ready period. A
                // genuine lifecycle boundary discards this pending capture.
                activeSave = current;
            }

            if (activeSave == null)
            {
                // Ready load with no readable/non-null current save (missing or throwing).
                return StateFailure("save-unavailable",
                    "current save unavailable during ready load", null, out stateReadFailed, out snapshot);
            }

            if (!ReferenceEquals(current, activeSave))
            {
                // A different save than the captured identity: never adopt silently. The
                // expected identity and readiness are retained so no budgets replenish.
                return StateFailure("save-reference-changed",
                    "active save reference changed unexpectedly", null, out stateReadFailed, out snapshot);
            }

            try
            {
                snapshot = reader.Read(activeSave);
            }
            catch (Exception ex)
            {
                return StateFailure("state-read", "fresh state read threw", ex, out stateReadFailed, out snapshot);
            }

            if (!snapshot.Readable)
            {
                return StateFailure("unreadable:" + snapshot.Failure,
                    snapshot.Failure ?? "state unreadable", null, out stateReadFailed, out snapshot);
            }

            // The state channel is complete on an actual readable snapshot, BEFORE any
            // corner/text work: a missing corner text now belongs to the corner category.
            ClearStateDiagnostic();
            return true;
        }

        /// <summary>
        /// Common state-failure path: fail closed on owned state-dependent outputs and
        /// preserve the expected load identity, readiness and the beacon episode. The
        /// exception detail (when present) is logged as technical detail only.
        /// </summary>
        private bool StateFailure(
            string key,
            string reason,
            Exception ex,
            out bool stateReadFailed,
            out SermonStateSnapshot snapshot)
        {
            stateReadFailed = true;
            snapshot = default;

            // A state-read failure suppresses all state-dependent displays (including the
            // beacon, whose eligibility cannot be evaluated without a readable state) and
            // preserves the expected load identity and readiness so recovery is automatic.
            SafeResetBeacon();
            hud.Teardown();
            ResetEligibility();
            LogStateDiagnostic(key, "GKSR_STATE_UNREADABLE: " + reason, ex);
            return false;
        }

        /// <summary>
        /// Corner stage: advances the beacon on the fresh readable snapshot, then renders
        /// the corner display. A missing required corner text, an actual binding/render
        /// failure or a Done-required failed icon set the corner flag; ordinary Pending,
        /// gates closed, a naturally absent HUD and an irrelevant icon failure do not.
        /// The corner flag is passed by reference so a computed Done-icon failure is never
        /// lost when the subsequent render reports Pending. This whole stage is contained:
        /// a corner presentation/native/log exception becomes a corner failure and never a
        /// state fault, and the healthy eligible beacon is preserved.
        /// </summary>
        private void TickCorner(SermonStateSnapshot snapshot, ref bool cornerHudFailed)
        {
            try
            {
                // The beacon advances before any corner/Done work, so on the first
                // LateUpdate after consumption a false eligibility clears it independently
                // of the corner text and the Done icon being Pending or Failed.
                UpdateBeacon(activeSave, snapshot);

                SermonDisplayState state = snapshot.Display;
                string text = snapshot.GatesSatisfied ? ResolveText(state, snapshot.Delta) : string.Empty;

                if (!snapshot.GatesSatisfied)
                {
                    // Gates not complete is a normal hidden state, not a fault; also the
                    // explicit end of this resource-eligibility episode.
                    ClearCornerDiagnostic();
                    hud.Suppress();
                    ResetEligibility();
                    return;
                }

                if (string.IsNullOrEmpty(text))
                {
                    // Missing approved required corner text: a corner-category fault.
                    // Never substitute a sentence; drop stale ownership and classify.
                    hud.Suppress();
                    ResetEligibility();
                    LogCornerDiagnostic("corner-text-missing:" + state,
                        "GKSR_HUD_FAILED: required corner text unavailable for " + state, null);
                    cornerHudFailed = true;
                    return;
                }

                LogDisplayTransition(snapshot);
                // Phase 1: resolve and validate the native host and style BEFORE any sprite
                // work, detecting a replaced or destroyed binding even while the Done icon
                // is pending or failed.
                SermonBindingResult binding = hud.PrepareBinding();
                if (binding.Rebound)
                {
                    ResetEligibility();
                }

                if (binding.Status == SermonHudStatus.Failed)
                {
                    // Actual binding failure: a corner-category fault. The binding failure
                    // already removed the owned presentation; the icon episode is kept.
                    ResetEligibility(preserveIconDiagnostic: true);
                    LogCornerDiagnostic("hud-failed:" + binding.Failure,
                        "GKSR_HUD_FAILED: " + binding.Failure, null);
                    cornerHudFailed = true;
                    return;
                }

                if (binding.Status != SermonHudStatus.Healthy)
                {
                    // The native HUD is simply not live right now (menu, loading or a
                    // rebuild): ordinary Pending, never a fault.
                    ResetEligibility();
                    return;
                }

                // Phase 2: bind is healthy, so poll the owned sprite. Off the sermon day it
                // is released; on the sermon day it is preloaded and retained.
                bool spriteEligible = state == SermonDisplayState.Ready || state == SermonDisplayState.Done;
                if (!spriteEligible)
                {
                    // Countdown day: no icon is expected. Detach the live Image's sprite
                    // reference before polling releases the handle.
                    hud.DetachOwnedIcon();
                }

                NativeCheckSpritePoll icon = checkSprite.Poll(spriteEligible);
                bool doneIconFailed = false;

                if (!spriteEligible || icon.Status == NativeCheckSpriteStatus.Ready)
                {
                    ClearIconDiagnostic();
                }
                else if (icon.Status == NativeCheckSpriteStatus.Failed && state == SermonDisplayState.Done)
                {
                    // A native Done icon failed AND the current display requires it: a
                    // corner-category fault. An unrelated icon failure on Ready/countdown
                    // is not a fault and never clears the required display. The flag is
                    // retained through the render result so a Pending suppression below
                    // never cancels this real fault.
                    string reason = checkSprite.LastFailure ?? "unknown";
                    LogCornerDiagnostic("icon-failed:" + reason,
                        "GKSR_HUD_FAILED: native done sprite unavailable (" + reason + ")", null);
                    doneIconFailed = true;
                }

                Sprite sprite = state == SermonDisplayState.Done && icon.Status == NativeCheckSpriteStatus.Ready
                    ? icon.Sprite
                    : null;

                SermonHudResult result = hud.Update(state, text, sprite);

                if (result.Status == SermonHudStatus.Failed)
                {
                    // Actual render failure: a corner-category fault; the episode is
                    // preserved until a completed healthy render or a boundary.
                    LogCornerDiagnostic("hud-render-failed:" + result.Failure,
                        "GKSR_HUD_FAILED: " + result.Failure, null);
                    cornerHudFailed = true;
                    return;
                }

                // A Done-required failed icon is a real corner fault even though it renders
                // as Pending; genuine Pending (icon still loading, or an ordinary absent
                // host) keeps cornerHudFailed false and is not a fault.
                if (doneIconFailed)
                {
                    cornerHudFailed = true;
                }

                if (result.Status == SermonHudStatus.Pending)
                {
                    // Suppressed because the Done icon is unavailable: the business state
                    // stays a readable Done and the in-flight handle is retained. Ordinary
                    // asset pending never clears a prior corner episode.
                    return;
                }

                // Healthy render: the only point where the corner episode is complete.
                ClearCornerDiagnostic();
            }
            catch (Exception ex)
            {
                // A corner text/native HUD/icon presentation or log exception is a corner
                // failure, never a state fault. Only corner-owned resources are suppressed
                // and the healthy eligible beacon is preserved.
                try { hud.Teardown(); }
                catch (Exception) { }
                ResetEligibility();
                LogCornerDiagnostic("corner-stage:" + ex.GetType().Name,
                    "GKSR_HUD_FAILED: corner stage " + ex.GetType().Name, ex);
                cornerHudFailed = true;
            }
        }

        /// <summary>
        /// Advance the owned beacon for this tick. Eligibility is derived from the fresh
        /// per-tick snapshot: the load-snapshot toggle, a readable state, satisfied
        /// gates, and the Ready display state (sermon day with the opportunity intact).
        /// Failure is contained so it can never propagate into the corner work.
        /// </summary>
        private void UpdateBeacon(GameSave save, SermonStateSnapshot snapshot)
        {
            bool eligible = beaconEnabledForLoad
                && snapshot.Readable
                && snapshot.GatesSatisfied
                && snapshot.Display == SermonDisplayState.Ready;

            BeaconUpdateResult result;
            try
            {
                result = beacon.Update(save, eligible);
            }
            catch (Exception ex)
            {
                // Defensive net: beacon.Update already contains recoverable errors.
                // Release owned resources but PRESERVE the beacon episode, so repeated
                // identical eligible failures deduplicate instead of re-logging per tick.
                ResetBeaconResources();
                LogBeaconDiagnostic("beacon-tick:" + ex.GetType().Name,
                    "GKSR_BEACON_FAILED: tick " + ex.GetType().Name);
                return;
            }

            switch (result.Status)
            {
                case BeaconStatus.Healthy:
                    // The only point where the beacon episode is complete.
                    ClearBeaconDiagnostic();
                    break;

                case BeaconStatus.Failed:
                    // Preserved across retries until a complete Draw or a reset boundary.
                    LogBeaconDiagnostic(
                        "beacon-failed:" + result.Failure + ":" + (beacon.LastFailure ?? "unknown"),
                        "GKSR_BEACON_FAILED: stage=" + result.Failure
                            + " reason=" + (beacon.LastFailure ?? "unknown"));
                    break;

                default:
                    // Inactive is a true not-eligible boundary; Pending only occurs while
                    // otherwise eligible (HUD/camera not live), so a Pending hold keeps
                    // the episode and only an actual eligibility loss clears it.
                    if (!eligible || result.Status == BeaconStatus.Inactive)
                        ClearBeaconDiagnostic();
                    break;
            }
        }

        internal void Shutdown()
        {
            enabled = false;
            SafeCleanup();
        }

        private static string ResolveText(SermonDisplayState state, int delta)
        {
            switch (state)
            {
                case SermonDisplayState.Countdown:
                    return BuildCountdownText(delta);
                case SermonDisplayState.Ready:
                    return SermonReminderLocalization.Get(SermonReminderLocalization.SermonReminderKey);
                case SermonDisplayState.Done:
                    return SermonReminderLocalization.Get(SermonReminderLocalization.SermonDoneKey);
                default:
                    return string.Empty;
            }
        }

        private static string BuildCountdownText(int delta)
        {
            if (delta < 1) return string.Empty;
            if (delta == 1)
                return SermonReminderLocalization.Get(SermonReminderLocalization.CountdownOneKey);

            string template = SermonReminderLocalization.Get(SermonReminderLocalization.CountdownOtherKey);
            return template.Replace("{days}", delta.ToString(CultureInfo.InvariantCulture));
        }

        private static GameSave TryGetCurrentSave()
        {
            try
            {
                return MainGame.Instance?.GameSave;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private void HandleLanguageChanged()
        {
            // Refresh style and text for the new language on the next tick.
            try
            {
                hud.MarkStyleDirty();
            }
            catch (Exception)
            {
                // Best-effort invalidation; the next tick repaints anyway.
            }
        }

        private void Unsubscribe()
        {
            if (!subscribed) return;
            subscribed = false;

            try
            {
                SaveSystem.OnSaveLoadingStarted -= HandleNativeLoadStarting;
                GameSettings.OnLanguageChanged -= HandleLanguageChanged;
            }
            catch (Exception)
            {
                // Event detach only; nothing else to undo.
            }
        }

        /// <summary>
        /// Reverse everything OnEnable applied and release the display. Wrapped so
        /// teardown runs even if one step fails, and can never fault the mod.
        /// </summary>
        private void SafeCleanup()
        {
            try { Unsubscribe(); }
            catch (Exception) { }

            try { harmony?.UnpatchSelf(); }
            catch (Exception) { }
            harmony = null;

            // Disable/shutdown is a genuine lifecycle boundary: close the notice load
            // period before dropping readiness so no stale notice can show afterwards.
            try { notices?.EndLoad(); }
            catch (Exception) { }
            ResetReadinessSafely();

            if (ReferenceEquals(Active, this)) Active = null;
        }

        /// <summary>
        /// Drop readiness and fully release the owned display and sprite handle.
        /// Used whenever the authoritative save may be replaced or the mod is torn
        /// down, so no stale presentation or resource can survive.
        /// </summary>
        private void ResetReadinessSafely()
        {
            ResetReadiness();
            ResetDiagnostics();
            SafeResetBeacon();
            try { hud.Teardown(); }
            catch (Exception) { }
            ResetEligibility();
        }

        private void ResetReadiness()
        {
            ready = false;
            activeSave = null;
            beaconEnabledForLoad = false;
        }

        /// <summary>
        /// One bounded, deduplicated corner-category diagnostic. It emits the approved
        /// user-facing explanation once per episode, then the technical stage/reason and
        /// the actual caught exception separately when one exists (never a fabricated
        /// stack for a logical failure). It stays on its own episode across intermediate
        /// healthy bindings and Pending phases and is cleared only after a completed
        /// healthy corner rendering or a genuine lifecycle/eligibility boundary. Every
        /// lookup and log is contained, including when called from an exception handler.
        /// </summary>
        private void LogCornerDiagnostic(string key, string message, Exception ex)
        {
            try
            {
                if (string.Equals(lastCornerDiagnostic, key, StringComparison.Ordinal)) return;
                lastCornerDiagnostic = key;
                LogCategoryDiagnostic(SermonReminderLocalization.DiagnosticsHudUnavailableKey);
                SafeWarn(message);
                LogExceptionDetail(ex);
            }
            catch (Exception) { }
        }

        private void ClearCornerDiagnostic() => lastCornerDiagnostic = null;

        /// <summary>
        /// One bounded diagnostic per distinct beacon failure episode, on its own
        /// channel. It is preserved across retries and Pending holds and cleared only
        /// on a Healthy complete Draw or at a true eligibility / load reset boundary.
        /// </summary>
        private void LogBeaconDiagnostic(string key, string message)
        {
            if (string.Equals(lastBeaconDiagnostic, key, StringComparison.Ordinal)) return;
            lastBeaconDiagnostic = key;
            try { log?.Warning(message); }
            catch (Exception) { }
        }

        private void ClearBeaconDiagnostic() => lastBeaconDiagnostic = null;

        /// <summary>Contained logger call for the newly added beacon paths.</summary>
        private void SafeLogError(string message)
        {
            try { log?.Error(message); }
            catch (Exception) { }
        }

        /// <summary>Contained info log: a logger failure never aborts cleanup or faults.</summary>
        private void SafeInfo(string message)
        {
            try { log?.Info(message); }
            catch (Exception) { }
        }

        /// <summary>Contained warning log: a logger failure never aborts cleanup or faults.</summary>
        private void SafeWarn(string message)
        {
            try { log?.Warning(message); }
            catch (Exception) { }
        }

        /// <summary>
        /// One bounded diagnostic per distinct state-read problem, deduplicated on
        /// its own channel and cleared only on an actual readable-state recovery. The
        /// approved category diagnostic text is logged separately from the technical
        /// GKSR stage and reason already carried in the message.
        /// </summary>
        private void LogStateDiagnostic(string key, string message, Exception ex)
        {
            try
            {
                if (string.Equals(lastStateDiagnostic, key, StringComparison.Ordinal)) return;
                lastStateDiagnostic = key;
                LogCategoryDiagnostic(SermonReminderLocalization.DiagnosticsReminderUnavailableKey);
                SafeWarn(message);
                LogExceptionDetail(ex);
            }
            catch (Exception) { }
        }

        private void ClearStateDiagnostic() => lastStateDiagnostic = null;

        /// <summary>
        /// Log the approved user-facing diagnostic sentence for a fault category, once
        /// per bounded failure episode, separately from the technical detail. A missing
        /// resource logs nothing here rather than inventing prose; the technical
        /// diagnostic is still emitted by the caller.
        /// </summary>
        private void LogCategoryDiagnostic(string diagnosticsKey)
        {
            string explanation;
            try { explanation = SermonReminderLocalization.Get(diagnosticsKey); }
            catch (Exception) { explanation = null; }

            if (!string.IsNullOrEmpty(explanation))
                SafeWarn(explanation);
        }

        /// <summary>
        /// Emit the actual caught exception as separate technical detail when one exists.
        /// A logical failure (no exception) logs nothing here rather than fabricating a
        /// stack. Contained so a logging failure can never escape a handler or abort cleanup.
        /// </summary>
        private void LogExceptionDetail(Exception ex)
        {
            if (ex == null) return;
            try { SafeWarn(ex.ToString()); }
            catch (Exception) { }
        }

        /// <summary>
        /// Clear the sprite-handle failure episode. Kept separate from the corner
        /// diagnostics: the sprite handle is a distinct channel, so its recovery never
        /// clears a corner render/text episode and vice versa.
        /// </summary>
        private void ClearIconDiagnostic() => lastIconDiagnostic = null;

        /// <summary>
        /// End the current resource-eligibility episode: release the owned sprite
        /// handle and drop its failure/backoff state. Called on every path where the
        /// mod stops needing the asset (gates closed, off-day, unreadable state, a
        /// replaced or failed host, readiness loss, shutdown), so a stale ownership
        /// and an old failure episode can never survive into unrelated work.
        ///
        /// <paramref name="preserveIconDiagnostic"/> keeps the icon channel's
        /// episode (used for a host failure: a broken asset stays broken across a
        /// rebind, so its warning must not be re-emitted as a new episode).
        /// </summary>
        private void ResetEligibility(bool preserveIconDiagnostic = false)
        {
            try { checkSprite.Release(); }
            catch (Exception) { }

            if (!preserveIconDiagnostic)
                lastIconDiagnostic = null;
        }

        /// <summary>Reset every diagnostic channel for a newly authoritative save.</summary>
        private void ResetDiagnostics()
        {
            lastStateDiagnostic = null;
            lastCornerDiagnostic = null;
            lastIconDiagnostic = null;
            lastBeaconDiagnostic = null;
            hasLoggedDisplay = false;
        }

        /// <summary>
        /// One bounded diagnostic per display-state change, carrying the actual
        /// absolute day and frame so a transition (e.g. Ready to Done after the
        /// sermon is consumed) is traceable without per-frame logging. Logging is
        /// contained: a logger failure never aborts the tick.
        /// </summary>
        private void LogDisplayTransition(SermonStateSnapshot snapshot)
        {
            if (hasLoggedDisplay && loggedDisplay == snapshot.Display) return;
            hasLoggedDisplay = true;
            loggedDisplay = snapshot.Display;
            SafeInfo("GKSR_DISPLAY: state=" + snapshot.Display
                + " day=" + snapshot.AbsoluteDay
                + " weekday=" + snapshot.DayOfWeek
                + " frame=" + SafeFrameCount());
        }

        private static int SafeFrameCount()
        {
            try
            {
                return Time.frameCount;
            }
            catch (Exception)
            {
                return -1;
            }
        }
    }
}
