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
using GK2.SermonReminder.Popup;
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

        // Stable opaque config identities and defaults for the two own toggles.
        // The beacon keeps its accepted identity/default/order; the popup toggle is
        // added with its own identity, default false and order 1. Both share the one
        // approved localized settings group.
        private const string BeaconSection = "Beacon";
        private const string BeaconKey = "EnabledChurchBeacon";
        private const string PopupSection = "Popup";
        private const string PopupKey = "EnabledSermonPopup";

        private readonly IReadOnlyList<Gk2ModDependency> dependencies;
        private readonly SermonStateReader reader = new SermonStateReader();
        private readonly SermonHudAdapter hud = new SermonHudAdapter();
        private readonly NativeCheckSprite checkSprite = new NativeCheckSprite();
        private readonly ChurchBeacon beacon = new ChurchBeacon();

        // One popup instance for the lifetime of this mod object, preserved across
        // ordinary menu/load/enable cycles rather than reconstructed per load or tick.
        private readonly SermonPopup popup = new SermonPopup();

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

        // The mod's own registered popup toggle, captured during registration as the
        // stable ConfigEntry value source. Null until registration AND own-descriptor
        // localization both succeed, which fails the popup closed (disabled) rather
        // than falling back to an unlocalized descriptor or a silent default-on.
        private ConfigEntry<bool> popupEntry;

        // Snapshot of the persisted popup toggle for the current load, established
        // exactly once at genuine readiness and immutable for that load. Runtime
        // changes only affect the next load, so process dedup is independent of it.
        private bool popupEnabledForLoad;

        // Independently deduplicated diagnostic channels: state read, corner
        // rendering (including the Done icon), the sprite handle and the beacon. Each
        // holds its own last-logged episode so a healthy channel never clears or
        // toggles another, and each is cleared only when that channel's own work
        // actually completes or at a true lifecycle / eligibility boundary.
        private string lastStateDiagnostic;
        private string lastCornerDiagnostic;
        private string lastBeaconDiagnostic;
        private string lastPopupDiagnostic;
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

            RegisterOwnSettings(context);
        }

        /// <summary>
        /// Register both own toggles and localize their presentations. Each feature is
        /// registered through the same owned transaction helper with its own identity,
        /// default and order; a failure in one is contained so the other still runs,
        /// and the failed feature stays closed (disabled) instead of silently
        /// defaulting on.
        /// </summary>
        private void RegisterOwnSettings(Gk2ModContext context)
        {
            // Fail closed until registration and own-descriptor wrapping both succeed.
            beaconEntry = null;
            popupEntry = null;

            Gk2Settings settings;
            try { settings = context?.Settings; }
            catch (Exception) { settings = null; }

            var groupTitle = new Func<string>(
                () => SermonReminderLocalization.Get(SermonReminderLocalization.SettingsGroupTitleKey));

            // Section/key/default/order and label/description catalog keys are the only
            // per-feature inputs to the shared registration transaction.
            OwnToggleRegistration beaconRegistration = LocalizedSettingRegistration.RegisterOwnToggle(
                settings,
                BeaconSection,
                BeaconKey,
                true,
                0,
                SermonReminderLocalization.SettingsBeaconLabelKey,
                SermonReminderLocalization.SettingsBeaconDescriptionKey,
                groupTitle);

            if (beaconRegistration.Succeeded)
            {
                beaconEntry = beaconRegistration.Entry;
            }
            else
            {
                SafeLogError("GKSR_BEACON_SETTING_FAILED: " + beaconRegistration.Failure);
            }

            OwnToggleRegistration popupRegistration = LocalizedSettingRegistration.RegisterOwnToggle(
                settings,
                PopupSection,
                PopupKey,
                false,
                1,
                SermonReminderLocalization.SettingsPopupLabelKey,
                SermonReminderLocalization.SettingsPopupDescriptionKey,
                groupTitle);

            if (popupRegistration.Succeeded)
            {
                popupEntry = popupRegistration.Entry;
            }
            else
            {
                SafeLogError("GKSR_POPUP_SETTING_FAILED: " + popupRegistration.Failure);
            }
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

            // Bind the existing Harmony owner for the popup provenance install. A
            // failure here is contained and never disables lifecycle hooks, HUD or
            // beacon; the popup itself fails closed until the observer is available.
            try { popup.ConfigureHarmony(harmony); }
            catch (Exception) { }

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

                // Snapshot the popup toggle exactly once for this genuine ready
                // transition, then begin the popup's current-load work against the
                // actual current native slot object captured at the entry prefix. The
                // mutable identity is never recaptured here.
                popupEnabledForLoad = ReadPopupEnabledSnapshot();
                BeginPopupLoad();

                // Open exactly one notice load period for this ready period, even when
                // the initial save read is missing or throws: a prior successful state
                // read is not required before a fault can be reported.
                try { notices?.BeginLoad(); }
                catch (Exception) { }

                ready = true;
                activeSave = null;
                ResetDiagnostics();

                // Best-effort initial capture; a null result without an exception leaves
                // the identity pending and a later readable save is adopted once for this
                // same ready period. A native read failure is preserved as its real
                // exception and fed to the bounded state diagnostic; readiness and the
                // already-opened notice budget are kept either way.
                GameSave save = TryGetCurrentSave(out Exception saveReadFailure);
                if (saveReadFailure != null)
                {
                    LogStateDiagnostic("save-read",
                        "GKSR_STATE_UNREADABLE: initial save read failed", saveReadFailure);
                }
                else if (save != null)
                {
                    activeSave = save;
                }

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
        /// Native load-entry prefix (ContinueGame): end the prior load, then freeze the
        /// immutable entry identity from the actual native slot argument. Every native
        /// read is contained here, never used unguarded as an argument outside it.
        /// </summary>
        internal void HandleLoadEntry(SaveSlotData saveSlotData)
        {
            HandleLifecycleEnd();
            try { popup.CaptureLoadIdentity(saveSlotData); }
            catch (Exception) { }
        }

        /// <summary>
        /// Native new-game load-entry prefix (CreateGameSaveAndStart): end the prior
        /// load, then freeze the identity from the native instance's already-allocated
        /// slot, contained so a failing getter never escapes into the native method.
        /// </summary>
        internal void HandleNewGameLoadEntry(MainGame instance)
        {
            HandleLifecycleEnd();
            try
            {
                SaveSlotData slot = instance != null ? instance.SaveSlotData : null;
                popup.CaptureLoadIdentity(slot);
            }
            catch (Exception) { }
        }

        /// <summary>
        /// Advance the process slot generation for an actual successful native slot
        /// deletion, bound to the exact owner captured per invocation.
        /// </summary>
        internal void HandleSuccessfulDeletion(string slotName, bool isDemoSave)
        {
            try { popup.RecordSuccessfulDeletion(slotName, isDemoSave); }
            catch (Exception) { }
        }

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

            // End the popup's current-load work independently; retained cleanup
            // continues to be driven by later ticks even after EndLoad.
            try { popup.EndLoad(); }
            catch (Exception) { }

            ResetReadinessSafely();
        }

        /// <summary>
        /// Begin the popup's current-load work with the once-per-load toggle snapshot
        /// and the actual current native slot object. Contained: a missing identity or
        /// a mismatched slot is failed closed by the popup itself.
        /// </summary>
        private void BeginPopupLoad()
        {
            try
            {
                SaveSlotData slot = MainGame.Instance?.SaveSlotData;
                popup.BeginLoad(slot, popupEnabledForLoad);
            }
            catch (Exception ex)
            {
                SafeLogError("GKSR_POPUP_LOAD_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Read the persisted popup toggle once for this load. A missing registration or
        /// a failed read is contained and fails the popup closed (disabled) for the
        /// load, never a silent default-on.
        /// </summary>
        private bool ReadPopupEnabledSnapshot()
        {
            try
            {
                ConfigEntry<bool> entry = popupEntry;
                if (entry == null)
                {
                    SafeLogError("GKSR_POPUP_SETTING_FAILED: toggle unavailable; popup disabled for this load");
                    return false;
                }

                // The successfully registered ConfigEntry is the stable value source.
                return entry.Value;
            }
            catch (Exception ex)
            {
                SafeLogError("GKSR_POPUP_SETTING_FAILED: read failed (" + ex.GetType().Name
                    + "); popup disabled for this load");
                return false;
            }
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
            bool beaconFailed = false;
            bool stateReady = false;

            // The popup is fed the same fresh snapshot the other reminders use. If the
            // state read does not succeed (or the tick never reaches it) this stays an
            // unreadable snapshot, so the popup never reuses a stale Ready/day.
            SermonStateSnapshot popupSnapshot = default;

            try
            {
                if (!enabled)
                {
                    // A disabled entry never starts a load or shows a notice. It still
                    // advances retained popup/notice cleanup through the single finally
                    // deliveries below, which cannot show while disabled.
                    return;
                }

                if (!TickState(out stateReadFailed, out SermonStateSnapshot snapshot))
                    return;

                popupSnapshot = snapshot;
                stateReady = true;
                TickCorner(snapshot, ref cornerHudFailed, ref beaconFailed);
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
                // Exactly one contained popup delivery per tick, independent of the
                // corner/state early returns, so an eligible popup is never skipped by a
                // missing corner text, a HUD/icon failure, or a healthy beacon update.
                // The popup always advances its own cleanup and reports its current
                // category failure independently of the corner channels.
                bool popupFailed = TickPopup(popupSnapshot);

                // Exactly one contained flag delivery per tick for every path, so a
                // buffered failure is never discarded by a scattered flush.
                try { notices?.Update(stateReadFailed, cornerHudFailed, beaconFailed, popupFailed); }
                catch (Exception) { }
            }
        }

        /// <summary>
        /// Deliver one contained popup evaluation for the fresh snapshot and return the
        /// popup category's current failure. The popup always advances its own cleanup,
        /// so a repaired transaction ends its diagnostic episode immediately even when
        /// the same tick then reports an ordinary Pending gate. A popup failure is never
        /// classified as a state or corner fault, and never suppresses the other
        /// reminders. Shared-state unreadability suppresses dependent popup notification
        /// eligibility without being reclassified as a popup fault.
        /// </summary>
        private bool TickPopup(SermonStateSnapshot snapshot)
        {
            bool failed = false;
            try
            {
                SermonPopupOutcome outcome = popup.Update(snapshot);

                // Current category failure: a concrete Failed outcome, or an ordinary
                // Pending hold whose owned failure episode is still unresolved
                // (retained cleanup or a failed attempt with no live transaction).
                failed = outcome.Status == SermonPopupStatus.Failed
                    || (outcome.Status == SermonPopupStatus.Pending && popup.HasActiveFailure);

                if (failed)
                {
                    if (outcome.Status == SermonPopupStatus.Failed)
                    {
                        // A concrete Failed outcome starts (or repeats) the bounded
                        // episode; the channel deduplicates on the exact episode key.
                        LogPopupDiagnostic(
                            "popup-failed:" + outcome.Stage + ":" + (outcome.Detail ?? "unknown"),
                            "GKSR_POPUP_FAILED: stage=" + outcome.Stage
                                + " detail=" + (outcome.Detail ?? "unknown"),
                            null);
                    }

                    // An ordinary Pending hold with an unresolved episode keeps the
                    // existing bounded diagnostic and is never re-logged per tick.
                }
                else
                {
                    // Healthy completion, a genuine eligibility boundary, or a repaired
                    // cleanup all end this bounded diagnostic episode.
                    ClearPopupDiagnostic();
                }
            }
            catch (Exception ex)
            {
                // The popup core contains every native failure; this is a defensive net.
                // It stays a popup-category diagnostic, never a state or corner fault.
                failed = true;
                LogPopupDiagnostic(
                    "popup-tick:" + ex.GetType().Name,
                    "GKSR_POPUP_FAILED: tick " + ex.GetType().Name,
                    ex);
            }

            // An unreadable shared state suppresses dependent popup eligibility: the
            // popup's own cleanup still advanced above, but no popup fault notice may be
            // fabricated from that suppression.
            return failed && snapshot.Readable;
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
        /// caller's single delivery. A state-read failure suppresses the state-dependent
        /// displays, including the beacon, whose eligibility cannot be evaluated without a
        /// readable state; only corner failures must preserve a healthy beacon.
        /// </summary>
        private bool TickState(out bool stateReadFailed, out SermonStateSnapshot snapshot)
        {
            stateReadFailed = false;
            snapshot = default;

            if (!ready)
            {
                // Not-ready tick: never a state fault. Cleanup is contained locally so a
                // throw here can never enter the outer state-fault classification; both
                // flags stay false and retained notice cleanup is still advanced by the
                // caller's single delivery.
                try
                {
                    SafeResetBeacon();
                    hud.Teardown();
                    ResetEligibility();
                }
                catch (Exception)
                {
                }
                return false;
            }

            // The helper preserves any real native exception instead of swallowing it, so
            // the actual detail reaches the bounded diagnostic; a genuine null (no
            // exception) is never turned into a fabricated one.
            GameSave current = TryGetCurrentSave(out Exception saveReadFailure);
            if (saveReadFailure != null)
            {
                return StateFailure("save-read", "current save read failed", saveReadFailure, out stateReadFailed, out snapshot);
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
        /// Common state-failure path: fail closed on owned state-dependent outputs,
        /// including resetting the state-dependent beacon, while preserving the expected
        /// load identity and readiness so recovery is automatic. The exception detail
        /// (when present) is logged as technical detail only.
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
        /// lost when the subsequent render reports Pending. The beacon category flag is
        /// carried independently by reference so a beacon fault computed before any
        /// corner early return is never lost. This whole stage is contained: a corner
        /// presentation/native/log exception becomes a corner failure and never a state
        /// fault, and the healthy eligible beacon is preserved.
        /// </summary>
        private void TickCorner(SermonStateSnapshot snapshot, ref bool cornerHudFailed, ref bool beaconFailed)
        {
            try
            {
                // The beacon advances before any corner/Done work, so on the first
                // LateUpdate after consumption a false eligibility clears it independently
                // of the corner text and the Done icon being Pending or Failed. The beacon
                // flag is delivered through the by-reference parameter on every path.
                UpdateBeacon(activeSave, snapshot, ref beaconFailed);

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
                    // already removed the owned presentation, so only the resource handle
                    // is released here.
                    ResetEligibility();
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
                string doneIconReason = null;

                if (spriteEligible && icon.Status != NativeCheckSpriteStatus.Ready
                    && checkSprite.HasActiveFailure && state == SermonDisplayState.Done)
                {
                    // A native Done icon failure episode is still active (including a
                    // bounded retry whose new request is pending) AND the current display
                    // requires it: a corner-category fault. Logging is DEFERRED until the
                    // render result is known so exactly one corner diagnostic is emitted per
                    // tick and an immediate render failure is never overwritten by the icon
                    // message (or vice versa) on the next frame.
                    doneIconReason = checkSprite.LastFailure ?? "unknown";
                    doneIconFailed = true;
                }

                Sprite sprite = state == SermonDisplayState.Done && icon.Status == NativeCheckSpriteStatus.Ready
                    ? icon.Sprite
                    : null;

                SermonHudResult result = hud.Update(state, text, sprite);

                if (result.Status == SermonHudStatus.Failed)
                {
                    // Actual render failure takes priority as this tick's single corner
                    // diagnostic; the episode is preserved until a completed healthy render
                    // or a boundary.
                    LogCornerDiagnostic("hud-render-failed:" + result.Failure,
                        "GKSR_HUD_FAILED: " + result.Failure, null);
                    cornerHudFailed = true;
                    return;
                }

                // A Done-required failed icon is a real corner fault even though it renders
                // as Pending; genuine Pending (icon still loading, or an ordinary absent
                // host) keeps cornerHudFailed false and is not a fault. With no render
                // failure this is this tick's single corner diagnostic.
                if (doneIconFailed)
                {
                    LogCornerDiagnostic("icon-failed:" + doneIconReason,
                        "GKSR_HUD_FAILED: native done sprite unavailable (" + doneIconReason + ")", null);
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
        /// The current beacon category failure is reported by reference as a concrete
        /// Failed outcome or an unresolved Pending hold, never from stale diagnostic
        /// text. Failure is contained so it can never propagate into the corner work.
        /// </summary>
        private void UpdateBeacon(GameSave save, SermonStateSnapshot snapshot, ref bool beaconFailed)
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
                // A true beacon escape is classified only as beacon.
                ResetBeaconResources();
                beaconFailed = true;
                LogBeaconDiagnostic("beacon-tick:" + ex.GetType().Name,
                    "GKSR_BEACON_FAILED: tick " + ex.GetType().Name, ex);
                return;
            }

            // Current category failure: a concrete Failed outcome, or an ordinary Pending
            // hold whose beacon failure episode is still unresolved (bounded retry
            // backoff or a temporarily absent native HUD). Recovery (a complete Draw or a
            // true ineligible/reset boundary) clears the episode in the module itself.
            if (result.Status == BeaconStatus.Failed
                || (result.Status == BeaconStatus.Pending && beacon.HasActiveFailure))
            {
                beaconFailed = true;
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
                            + " reason=" + (beacon.LastFailure ?? "unknown"),
                        null);
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

        private static GameSave TryGetCurrentSave(out Exception failure)
        {
            failure = null;
            try
            {
                return MainGame.Instance?.GameSave;
            }
            catch (Exception ex)
            {
                // Preserve the real native exception instead of swallowing it, so the
                // bounded diagnostic can carry its actual detail. A successful native
                // null is returned as-is and never turned into a fabricated exception.
                failure = ex;
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

            // Drop the provenance configuration; UnpatchSelf above already removed the
            // transpiler for this owner, so a re-enable must re-verify registration.
            try { popup.ClearHarmonyConfiguration(); }
            catch (Exception) { }

            // Disable/shutdown is a genuine lifecycle boundary: close the notice load
            // period before dropping readiness so no stale notice can show afterwards.
            try { notices?.EndLoad(); }
            catch (Exception) { }

            // End the popup's current-load work; retained cleanup keeps being driven by
            // later ticks rather than being discarded here.
            try { popup.EndLoad(); }
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
            popupEnabledForLoad = false;
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
        /// channel. It emits the approved user-facing explanation separately from the
        /// technical stage/reason and the real caught exception detail when one exists
        /// (never a fabricated stack for a logical failure). It is preserved across
        /// retries and Pending holds and cleared only on a Healthy complete Draw or at a
        /// true eligibility / load reset boundary. Every lookup and log is contained.
        /// </summary>
        private void LogBeaconDiagnostic(string key, string message, Exception ex)
        {
            try
            {
                if (string.Equals(lastBeaconDiagnostic, key, StringComparison.Ordinal)) return;
                lastBeaconDiagnostic = key;
                LogCategoryDiagnostic(SermonReminderLocalization.DiagnosticsBeaconUnavailableKey);
                SafeWarn(message);
                LogExceptionDetail(ex);
            }
            catch (Exception) { }
        }

        private void ClearBeaconDiagnostic() => lastBeaconDiagnostic = null;

        /// <summary>
        /// One bounded diagnostic per distinct popup failure episode, on its own channel.
        /// It emits the approved user-facing explanation separately from the technical
        /// stage/detail and the real caught exception detail when one exists. It is
        /// preserved while the popup retains a current failure and cleared on an actual
        /// Healthy completion, a true eligibility boundary, or a repaired cleanup. It is
        /// never a state or corner category.
        /// </summary>
        private void LogPopupDiagnostic(string key, string message, Exception ex)
        {
            try
            {
                if (string.Equals(lastPopupDiagnostic, key, StringComparison.Ordinal)) return;
                lastPopupDiagnostic = key;
                LogCategoryDiagnostic(SermonReminderLocalization.DiagnosticsPopupUnavailableKey);
                SafeWarn(message);
                LogExceptionDetail(ex);
            }
            catch (Exception) { }
        }

        private void ClearPopupDiagnostic() => lastPopupDiagnostic = null;

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
        /// End the current resource-eligibility episode: release the owned sprite
        /// handle and drop its failure/backoff state. Called on every path where the
        /// mod stops needing the asset (gates closed, off-day, unreadable state, a
        /// replaced or failed host, readiness loss, shutdown), so a stale ownership
        /// and an old failure episode can never survive into unrelated work.
        /// </summary>
        private void ResetEligibility()
        {
            try { checkSprite.Release(); }
            catch (Exception) { }
        }

        /// <summary>Reset every diagnostic channel for a newly authoritative save.</summary>
        private void ResetDiagnostics()
        {
            lastStateDiagnostic = null;
            lastCornerDiagnostic = null;
            lastBeaconDiagnostic = null;
            lastPopupDiagnostic = null;
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
