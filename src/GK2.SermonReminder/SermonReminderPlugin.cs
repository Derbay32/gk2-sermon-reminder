using System;
using System.Collections.Generic;
using System.Globalization;
using BepInEx;
using GK2.Framework;
using GK2.SermonReminder.Hud;
using GK2.SermonReminder.Localization;
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

        private readonly IReadOnlyList<Gk2ModDependency> dependencies;
        private readonly SermonStateReader reader = new SermonStateReader();
        private readonly SermonHudAdapter hud = new SermonHudAdapter();
        private readonly NativeCheckSprite checkSprite = new NativeCheckSprite();

        private Gk2ModMetadata metadata;
        private string metadataLanguage;

        private Gk2ModLogger log;
        private Harmony harmony;
        private bool subscribed;
        private bool enabled;

        private bool ready;
        private GameSave activeSave;

        // Independently deduplicated diagnostic channels: state read, native HUD
        // binding, and the icon module. Each holds its own last-logged episode so
        // the healthy channels never clear or toggle another channel, and each is
        // cleared only when that channel's own work actually completes.
        private string lastStateDiagnostic;
        private string lastHudDiagnostic;
        private string lastIconDiagnostic;
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
                log.Info("GKSR_REGISTERED: version=" + SermonReminderPlugin.PluginVersion);
            }
            catch (Exception)
            {
                // A registration failure would fault the mod; keep registration safe.
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
                log?.Error("GKSR_PATCH_FAILED: " + ex.GetType().Name);
            }

            try
            {
                SaveSystem.OnSaveLoadingStarted += HandleNativeLoadStarting;
                GameSettings.OnLanguageChanged += HandleLanguageChanged;
                subscribed = true;
            }
            catch (Exception ex)
            {
                log?.Error("GKSR_SUBSCRIBE_FAILED: " + ex.GetType().Name);
            }

            log?.Info("GKSR_ENABLED");
        }

        public override void OnDisable()
        {
            enabled = false;
            SafeCleanup();
            log?.Info("GKSR_DISABLED");
        }

        public override void OnGameStarted()
        {
            // Readiness is only ever established here. MainGame.gameState becomes
            // InGame before scene loading finishes, so it is not sufficient; the
            // save is captured only from this framework callback.
            try
            {
                GameSave save = MainGame.Instance?.GameSave;
                if (save == null)
                {
                    ResetReadinessSafely();
                    LogStateDiagnostic("no-save-after-start",
                        "GKSR_READY_FAILED: no active save after OnGameStarted");
                    return;
                }

                activeSave = save;
                ready = true;
                ResetDiagnostics();
                log.Info("GKSR_READY");
            }
            catch (Exception ex)
            {
                ResetReadinessSafely();
                LogStateDiagnostic("game-started:" + ex.GetType().Name,
                    "GKSR_READY_FAILED: " + ex.GetType().Name);
            }
        }

        public override void OnReturnedToMainMenu() => HandleReturnedToMenu();

        /// <summary>
        /// Called by early prefixes and SaveSystem.OnSaveLoadingStarted before a
        /// new save becomes authoritative. Contained so a Harmony prefix can never
        /// throw into the native method.
        /// </summary>
        internal void HandleNativeLoadStarting() => ResetReadinessSafely();

        internal void HandleReturnedToMenu() => ResetReadinessSafely();

        internal void Tick()
        {
            if (!enabled)
                return;

            // LateUpdate is invoked by Unity, not by the framework, so every
            // native read/HUD operation is contained here: a recoverable error
            // hides the display and never faults the mod. The icon module contains
            // its own native asset access, so an icon failure never reaches here.
            try
            {
                TickCore();
            }
            catch (Exception ex)
            {
                // A failure this broad is not attributable to the icon module, so
                // it tears down the display and drops the sprite we own.
                hud.Teardown();
                ResetEligibility();
                LogStateDiagnostic("tick:" + ex.GetType().Name, "GKSR_TICK_FAILED: " + ex.GetType().Name);
            }
        }

        private void TickCore()
        {
            if (!ready || activeSave == null)
            {
                hud.Teardown();
                ResetEligibility();
                return;
            }

            GameSave current = TryGetCurrentSave();
            if (current == null)
            {
                hud.Teardown();
                ResetEligibility();
                return;
            }

            if (!ReferenceEquals(current, activeSave))
            {
                // The active save changed without a proper readiness transition.
                // Hide and wait for OnGameStarted rather than reuse stale data.
                ResetReadiness();
                hud.Teardown();
                ResetEligibility();
                LogStateDiagnostic("save-reference-changed",
                    "GKSR_STATE_UNREADABLE: active save reference changed unexpectedly");
                return;
            }

            SermonStateSnapshot snapshot = reader.Read(activeSave);
            if (!snapshot.Readable)
            {
                // No assumed business state: fail closed, hide and drop the owned
                // resources so a fault can never reuse the previous judgement.
                hud.Teardown();
                ResetEligibility();
                LogStateDiagnostic("unreadable:" + snapshot.Failure,
                    "GKSR_STATE_UNREADABLE: " + snapshot.Failure);
                return;
            }

            SermonDisplayState state = snapshot.Display;
            string text = snapshot.GatesSatisfied ? ResolveText(state, snapshot.Delta) : string.Empty;

            if (!snapshot.GatesSatisfied)
            {
                // Gates not complete is a normal hidden state, not a fault. It is
                // also the explicit end of this resource-eligibility episode: the
                // owned sprite request is not needed while the gates are closed, and
                // the state channel is reset here because there is no text work.
                ClearStateDiagnostic();
                hud.Suppress();
                ResetEligibility();
                return;
            }

            // The state/text channel only completes once a real sentence exists.
            // Clearing it on the readable snapshot alone would drop the episode and
            // re-log the same missing-text warning on every following frame.
            if (string.IsNullOrEmpty(text))
            {
                // A missing/empty localized sentence is an unreadable resource,
                // not a reason to show an empty active label. We never substitute a
                // replacement sentence, and any stale ownership is still dropped so
                // it cannot survive the resource-unavailable transition.
                hud.Suppress();
                ResetEligibility();
                LogStateDiagnostic("missing-text:" + state,
                    "GKSR_TEXT_UNAVAILABLE: localized " + state + " text is empty");
                return;
            }

            ClearStateDiagnostic();
            LogDisplayTransition(snapshot);
            // Phase 1: resolve and validate the native host and style BEFORE any
            // sprite work. This detects a replaced or destroyed binding and runs
            // even while the Done icon is pending or failed, so a HUD rebuild is
            // never missed during a load.
            SermonBindingResult binding = hud.PrepareBinding();
            if (binding.Rebound)
            {
                // The previous binding is gone: drop the old sprite ownership before
                // acquiring one for the new binding, so its sprite can never be
                // assigned to the replacement display.
                ResetEligibility();
            }

            if (binding.Status == SermonHudStatus.Failed)
            {
                // The binding failure already removed the owned presentation. The
                // sprite request is not needed without a host to render into. The
                // icon episode is kept: a broken asset stays broken across a rebind,
                // so the same warning is not repeated for it.
                ResetEligibility(preserveIconDiagnostic: true);
                LogHudDiagnostic("hud-failed:" + binding.Failure, "GKSR_HUD_FAILED: " + binding.Failure);
                return;
            }

            if (binding.Status != SermonHudStatus.Healthy)
            {
                // The native HUD is simply not live right now (menu, loading or a
                // rebuild): ordinary unreadiness, never reported as a fault, and an
                // explicit end of the eligibility episode (the host is absent).
                ResetEligibility();
                return;
            }

            // Phase 2: bind is healthy, so poll the owned sprite. Off the sermon day
            // it is released; on the sermon day it is preloaded and retained while
            // Ready or Done so consuming the sermon does not start a cold load.
            bool spriteEligible = state == SermonDisplayState.Ready || state == SermonDisplayState.Done;
            NativeCheckSpritePoll icon = checkSprite.Poll(spriteEligible);

            if (!spriteEligible)
            {
                // Countdown day: no icon is expected, so this is an explicit end of
                // the eligibility episode. The released asset closes its episode
                // here instead of carrying a failure into the next sermon day.
                ClearIconDiagnostic();
            }
            else if (icon.Status == NativeCheckSpriteStatus.Ready)
            {
                ClearIconDiagnostic();
            }
            else if (icon.Status == NativeCheckSpriteStatus.Failed)
            {
                string reason = checkSprite.LastFailure ?? "unknown";
                LogIconDiagnostic("icon-failed:" + reason,
                    "GKSR_ICON_FAILED: native done sprite unavailable (" + reason + "); icon retries while eligible");
            }

            Sprite sprite = state == SermonDisplayState.Done && icon.Status == NativeCheckSpriteStatus.Ready
                ? icon.Sprite
                : null;

            SermonHudResult result = hud.Update(state, text, sprite);
            if (result.Status == SermonHudStatus.Failed)
            {
                // A render failure removed the owned presentation synchronously. The
                // channel stays on its own episode key, so a persisting failure is
                // logged once and only a real recovery clears it.
                LogHudDiagnostic("hud-render-failed:" + result.Failure, "GKSR_HUD_FAILED: " + result.Failure);
                return;
            }

            if (result.Status == SermonHudStatus.Pending)
            {
                // Suppressed because the Done icon is unavailable: the business
                // state stays a readable Done and the in-flight handle is retained.
                // Ordinary asset pending must not clear a prior render-failure
                // episode, so the HUD channel is intentionally left untouched.
                return;
            }

            // Healthy render: the only point where the binding channel is complete.
            ClearHudDiagnostic();
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
            try { hud.Teardown(); }
            catch (Exception) { }
            ResetEligibility();
        }

        private void ResetReadiness()
        {
            ready = false;
            activeSave = null;
        }

        /// <summary>
        /// One bounded diagnostic per distinct state-read problem, deduplicated on
        /// its own channel and cleared only on an actual readable-state recovery.
        /// </summary>
        private void LogStateDiagnostic(string key, string message)
        {
            if (string.Equals(lastStateDiagnostic, key, StringComparison.Ordinal)) return;
            lastStateDiagnostic = key;
            log?.Warning(message);
        }

        private void ClearStateDiagnostic() => lastStateDiagnostic = null;

        /// <summary>
        /// One bounded diagnostic per distinct native-HUD binding problem, on its
        /// own channel so icon activity never clears it and vice versa.
        /// </summary>
        private void LogHudDiagnostic(string key, string message)
        {
            if (string.Equals(lastHudDiagnostic, key, StringComparison.Ordinal)) return;
            lastHudDiagnostic = key;
            log?.Warning(message);
        }

        private void ClearHudDiagnostic() => lastHudDiagnostic = null;

        /// <summary>
        /// One bounded diagnostic per icon failure episode: it stays logged while
        /// the same failure persists (including across retries) and is cleared only
        /// when the sprite actually recovers. Ordinary async pending is never logged.
        /// </summary>
        private void LogIconDiagnostic(string key, string message)
        {
            if (string.Equals(lastIconDiagnostic, key, StringComparison.Ordinal)) return;
            lastIconDiagnostic = key;
            log?.Warning(message);
        }

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
            lastHudDiagnostic = null;
            lastIconDiagnostic = null;
            hasLoggedDisplay = false;
        }

        /// <summary>
        /// One bounded diagnostic per display-state change, carrying the actual
        /// absolute day and frame so a transition (e.g. Ready to Done after the
        /// sermon is consumed) is traceable without per-frame logging.
        /// </summary>
        private void LogDisplayTransition(SermonStateSnapshot snapshot)
        {
            if (hasLoggedDisplay && loggedDisplay == snapshot.Display) return;
            hasLoggedDisplay = true;
            loggedDisplay = snapshot.Display;
            log?.Info("GKSR_DISPLAY: state=" + snapshot.Display
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
