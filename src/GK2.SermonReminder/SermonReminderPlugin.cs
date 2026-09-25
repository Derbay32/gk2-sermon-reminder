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

        private string lastDiagnostic;
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
                    LogTransition("no-save-after-start", "GKSR_READY_FAILED: no active save after OnGameStarted");
                    return;
                }

                activeSave = save;
                ready = true;
                lastDiagnostic = null;
                log.Info("GKSR_READY");
            }
            catch (Exception ex)
            {
                ResetReadinessSafely();
                LogTransition("game-started:" + ex.GetType().Name,
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
            // hides the display and never faults the mod.
            try
            {
                TickCore();
            }
            catch (Exception ex)
            {
                hud.Teardown();
                checkSprite.Release();
                LogTransition("tick:" + ex.GetType().Name,
                    "GKSR_TICK_FAILED: " + ex.GetType().Name);
            }
        }

        private void TickCore()
        {
            if (!ready || activeSave == null)
            {
                hud.Teardown();
                checkSprite.Release();
                return;
            }

            GameSave current = TryGetCurrentSave();
            if (current == null)
            {
                hud.Teardown();
                checkSprite.Release();
                return;
            }

            if (!ReferenceEquals(current, activeSave))
            {
                // The active save changed without a proper readiness transition.
                // Hide and wait for OnGameStarted rather than reuse stale data.
                ResetReadiness();
                hud.Teardown();
                checkSprite.Release();
                LogTransition("save-reference-changed",
                    "GKSR_STATE_UNREADABLE: active save reference changed unexpectedly");
                return;
            }

            SermonStateSnapshot snapshot = reader.Read(activeSave);
            if (!snapshot.Readable)
            {
                // No assumed business state: fail closed, hide and drop the owned
                // resources so a fault can never reuse the previous judgement.
                hud.Teardown();
                checkSprite.Release();
                LogTransition("unreadable:" + snapshot.Failure,
                    "GKSR_STATE_UNREADABLE: " + snapshot.Failure);
                return;
            }

            LogDisplayTransition(snapshot);

            if (!snapshot.GatesSatisfied)
            {
                // Gates not complete is a normal hidden state, not a fault. The
                // owned sprite request is not needed while the gates are closed.
                hud.Hide();
                checkSprite.Release();
                ClearDiagnostic();
                return;
            }

            SermonDisplayState state = snapshot.Display;

            // Preload and retain the single owned sprite while the sermon day is
            // Ready or Done, so consuming the sermon does not start a cold load.
            // Off the sermon day the handle is released.
            bool spriteEligible = state == SermonDisplayState.Ready || state == SermonDisplayState.Done;
            NativeCheckSpritePoll icon = checkSprite.Poll(spriteEligible);

            if (icon.Status == NativeCheckSpriteStatus.Failed)
            {
                LogTransition("icon-failed:" + checkSprite.LastFailure,
                    "GKSR_ICON_FAILED: native done sprite unavailable (" + checkSprite.LastFailure + ")");
            }

            string text = ResolveText(state, snapshot.Delta);
            if (string.IsNullOrEmpty(text))
            {
                // A missing/empty localized sentence is an unreadable resource,
                // not a reason to show an empty active label. We never substitute
                // a replacement sentence.
                hud.Hide();
                LogTransition("missing-text:" + state,
                    "GKSR_TEXT_UNAVAILABLE: localized " + state + " text is empty");
                return;
            }

            if (state == SermonDisplayState.Done && icon.Status != NativeCheckSpriteStatus.Ready)
            {
                // The icon is still pending or failed: suppress the whole dependent
                // Done presentation (text and icon) while the business state stays
                // a readable Done. The owned in-flight handle is retained, so we do
                // not reload on every frame.
                hud.Hide();
                LogTransition("done-icon-suppressed:" + icon.Status,
                    "GKSR_DONE_ICON_PENDING: Done presentation suppressed (" + icon.Status + ")");
                return;
            }

            Sprite sprite = state == SermonDisplayState.Done ? icon.Sprite : null;
            SermonHudResult result = hud.Update(state, text, sprite);

            if (result.Rebound)
            {
                // The native HUD was rebuilt: the pre-rebuild owned sprite
                // reference is released here and the new binding re-requests it.
                checkSprite.Release();
            }

            if (result.Status == SermonHudStatus.Failed)
            {
                LogTransition("hud-failed:" + result.Failure, "GKSR_HUD_FAILED: " + result.Failure);
                return;
            }

            if (result.Status == SermonHudStatus.Pending)
            {
                // The native HUD is simply not live right now (loading, menu or a
                // rebuild): ordinary unreadiness, never reported as a fault.
                ClearDiagnostic();
                return;
            }

            ClearDiagnostic();
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
            try { hud.Teardown(); }
            catch (Exception) { }
            try { checkSprite.Release(); }
            catch (Exception) { }
            hasLoggedDisplay = false;
        }

        private void ResetReadiness()
        {
            ready = false;
            activeSave = null;
        }

        /// <summary>
        /// One bounded diagnostic per distinct problem, cleared once the display
        /// recovers, so a persistent condition never spams the log per frame.
        /// </summary>
        private void LogTransition(string key, string message)
        {
            if (string.Equals(lastDiagnostic, key, StringComparison.Ordinal)) return;
            lastDiagnostic = key;
            log?.Warning(message);
        }

        private void ClearDiagnostic() => lastDiagnostic = null;

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
