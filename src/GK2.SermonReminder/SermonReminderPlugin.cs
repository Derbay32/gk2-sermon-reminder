using System;
using System.Collections.Generic;
using System.Globalization;
using BepInEx;
using GK2.Framework;
using GK2.SermonReminder.Hud;
using GK2.SermonReminder.Localization;
using GK2.SermonReminder.State;
using HarmonyLib;

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
    /// Owns the non-sermon-day countdown lifecycle: readiness, fresh per-tick
    /// state reads and the single mod-owned HUD label.
    /// </summary>
    internal sealed class SermonReminderMod : Gk2ModBase
    {
        internal static SermonReminderMod Active { get; private set; }

        private readonly IReadOnlyList<Gk2ModDependency> dependencies;
        private readonly SermonStateReader reader = new SermonStateReader();
        private readonly CountdownHudAdapter hud = new CountdownHudAdapter();

        private Gk2ModMetadata metadata;
        private string metadataLanguage;

        private Gk2ModLogger log;
        private Harmony harmony;
        private bool subscribed;
        private bool enabled;

        private bool ready;
        private GameSave activeSave;

        private string lastDiagnostic;

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
                    ResetReadiness();
                    hud.Teardown();
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
                ResetReadiness();
                hud.Teardown();
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
        internal void HandleNativeLoadStarting()
        {
            ResetReadinessSafely();
            lastDiagnostic = null;
        }

        internal void HandleReturnedToMenu()
        {
            ResetReadinessSafely();
            lastDiagnostic = null;
        }

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
                LogTransition("tick:" + ex.GetType().Name,
                    "GKSR_TICK_FAILED: " + ex.GetType().Name);
            }
        }

        private void TickCore()
        {
            if (!ready || activeSave == null)
            {
                hud.Teardown();
                return;
            }

            GameSave current = TryGetCurrentSave();
            if (current == null)
            {
                hud.Teardown();
                return;
            }

            if (!ReferenceEquals(current, activeSave))
            {
                // The active save changed without a proper readiness transition.
                // Hide and wait for OnGameStarted rather than reuse stale data.
                ResetReadiness();
                hud.Teardown();
                LogTransition("save-reference-changed", "GKSR_STATE_UNREADABLE: active save reference changed unexpectedly");
                return;
            }

            SermonStateSnapshot snapshot = reader.Read(activeSave);
            if (!snapshot.Readable)
            {
                hud.Hide();
                LogTransition("unreadable:" + snapshot.Failure, "GKSR_STATE_UNREADABLE: " + snapshot.Failure);
                return;
            }

            if (!snapshot.GatesSatisfied)
            {
                // Gates not complete is a normal hidden state, not a fault.
                hud.Hide();
                lastDiagnostic = null;
                return;
            }

            int delta = snapshot.Delta;
            if (delta <= 0 || delta > snapshot.DaysInWeek - 1)
            {
                // delta 0 is the sermon day itself: hide completely in this ticket.
                hud.Hide();
                lastDiagnostic = null;
                return;
            }

            string text = BuildCountdownText(delta);
            if (string.IsNullOrEmpty(text))
            {
                // A missing/empty localized sentence is an unreadable resource,
                // not a reason to show an empty active label. We never substitute
                // a replacement sentence.
                hud.Hide();
                LogTransition("missing-text", "GKSR_TEXT_UNAVAILABLE: localized countdown text is empty; countdown suppressed");
                return;
            }

            if (!hud.Update(text))
            {
                LogTransition("hud-unavailable", "GKSR_HUD_UNAVAILABLE: native HUD label host not ready; countdown suppressed");
                return;
            }

            lastDiagnostic = null;
        }

        internal void Shutdown()
        {
            enabled = false;
            SafeCleanup();
        }

        private static string BuildCountdownText(int delta)
        {
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

        private void ResetReadinessSafely()
        {
            ResetReadiness();
            try { hud.Teardown(); }
            catch (Exception) { }
        }

        private void ResetReadiness()
        {
            ready = false;
            activeSave = null;
        }

        private void LogTransition(string key, string message)
        {
            if (string.Equals(lastDiagnostic, key, StringComparison.Ordinal)) return;
            lastDiagnostic = key;
            log?.Warning(message);
        }
    }
}
