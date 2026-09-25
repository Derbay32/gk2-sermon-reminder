using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using GK2.SermonReminder.Localization;
using GK2.SermonReminder.State;
using HarmonyLib;
using LazyBearTechnology;
using TMPro;
using UnityEngine;

namespace GK2.SermonReminder.Popup
{
    /// <summary>
    /// Outcome of a single popup evaluation. This is diagnostics only; a display
    /// claim is recorded inside the popup once the native dialog is confirmed
    /// actually shown.
    /// </summary>
    internal enum SermonPopupStatus
    {
        /// <summary>No current-load popup owns the gate: not loaded, disabled, or not eligible.</summary>
        Inactive,

        /// <summary>Eligible and awaiting the native UI safety gate, or backing off a bounded retry.</summary>
        Pending,

        /// <summary>An owned native dialog is actually shown and the day is recorded.</summary>
        Healthy,

        /// <summary>A concrete stage failed; the chance is retained and a bounded retry is scheduled.</summary>
        Failed
    }

    /// <summary>Stage that produced a <see cref="SermonPopupStatus.Failed"/> outcome.</summary>
    internal enum SermonPopupStage
    {
        None,
        Identity,
        Localization,
        Resolve,
        Claim,
        Open,
        Show,
        Cleanup
    }

    internal readonly struct SermonPopupOutcome
    {
        internal SermonPopupStatus Status { get; }
        internal SermonPopupStage Stage { get; }
        internal string Detail { get; }

        internal SermonPopupOutcome(SermonPopupStatus status, SermonPopupStage stage, string detail)
        {
            Status = status;
            Stage = stage;
            Detail = detail;
        }
    }

    /// <summary>
    /// Isolated sermon-day confirmation popup.
    ///
    /// Ownership model: the popup borrows the shared, game-managed
    /// <see cref="UIDialogWindow"/> instance returned by
    /// <see cref="LazyUI.GetWindow{T}"/> and the protected
    /// <c>LazyWidget&lt;UIDialogWindowData&gt;.data</c> slot, which is claimed with
    /// our own <see cref="UIDialogWindowData"/> before the native
    /// <c>Open</c> runs. Every native open is treated as a transaction that may
    /// fail part-way. Content is only ever cleared or closed while the window's
    /// current data is reference-equal to our own data; if another system takes
    /// the window over we drop our native references and clear only our managed
    /// callbacks. The cached window is never destroyed, cloned or re-initialised,
    /// and no native pool object is ever destroyed.
    ///
    /// De-duplication identity (slot name, demo flag, process slot generation) is
    /// frozen once per load in <see cref="CaptureLoadIdentity"/>; the concrete
    /// game day is read fresh from every <see cref="Update"/> call. A record is
    /// written only after the native dialog is confirmed actually displayed, and
    /// the process-memory maps are never cleared by reload, menu round-trip,
    /// overwrite, deletion or a settings toggle.
    /// </summary>
    internal sealed class SermonPopup
    {
        // Exact catalog keys; the sentences live only in the localization catalog.
        private const string TitleKey = "gksr.popup.sermon.title";
        private const string BodyKey = "gksr.popup.sermon.body";
        private const string ConfirmKey = "gksr.popup.sermon.confirm";

        // Bounded unscaled retry after a failed native transaction.
        private const float RetryDelaySeconds = 2f;

        // Exact native members. LazyWindow/LazyWidget are generic, so the fields
        // are addressed on the concrete window and resolved through base types.
        private static readonly FieldInfo DialogDataField = AccessTools.Field(typeof(UIDialogWindow), "data");
        private static readonly FieldInfo OnClosedField = AccessTools.Field(typeof(UIDialogWindow), "OnClosed");
        private static readonly FieldInfo HeaderField = AccessTools.Field(typeof(UIDialogWindow), "header");
        private static readonly FieldInfo InformationField = AccessTools.Field(typeof(UIDialogWindow), "information");
        private static readonly FieldInfo ButtonsContentField = AccessTools.Field(typeof(UIDialogWindow), "buttonsContent");
        private static readonly FieldInfo ActiveButtonsField = AccessTools.Field(typeof(UIDialogWindow), "activeButtons");
        private static readonly FieldInfo ButtonPrefabField = AccessTools.Field(typeof(UIDialogWindow), "buttonPrefab");
        private static readonly FieldInfo ButtonLabelField = AccessTools.Field(typeof(UIDialogWindowButton), "label");

        // Process-memory only: generations per immutable native slot key, and the
        // per-(frozen identity, day) display records. Never persisted, never
        // cleared by reload/menu/overwrite/toggle.
        private readonly Dictionary<NativeSlotKey, int> generationBySlot = new Dictionary<NativeSlotKey, int>();
        private readonly HashSet<DedupKey> dedupRecords = new HashSet<DedupKey>();

        // Frozen load identity.
        private bool hasIdentity;
        private SermonLoadIdentity identity;
        private SaveSlotData frozenSlot;
        private string identityFailure;

        // Current-load work.
        private bool enabled;
        private bool pending;
        private bool shown;
        private bool retryScheduled;
        private float retryNotBeforeUnscaled;

        // Owned native transaction.
        private UIDialogWindow ownedWindow;
        private UIDialogWindowData ownedData;
        private Action<UIDialogWindowData> ownedCallback;

        internal SermonPopupStatus LastStatus { get; private set; }
        internal SermonPopupStage LastStage { get; private set; }

        /// <summary>True while this load has queued (not yet displayed) popup work.</summary>
        internal bool IsPending => pending;

        /// <summary>
        /// Freeze the immutable native slot key and generation for a load, before
        /// any native mutation. The generation is read from the process map here,
        /// so a later successful deletion only affects future captures. Contained.
        /// </summary>
        internal void CaptureLoadIdentity(SaveSlotData slot)
        {
            try
            {
                if (slot == null)
                {
                    hasIdentity = false;
                    frozenSlot = null;
                    identityFailure = "null-slot";
                    return;
                }

                string slotName = slot.slotName;
                if (string.IsNullOrEmpty(slotName))
                {
                    hasIdentity = false;
                    frozenSlot = null;
                    identityFailure = "empty-slot-name";
                    return;
                }

                bool isDemoSave = slot.isDemoSave;
                var key = new NativeSlotKey(slotName, isDemoSave);
                int generation = generationBySlot.TryGetValue(key, out int current) ? current : 0;

                identity = new SermonLoadIdentity(slotName, isDemoSave, generation);
                frozenSlot = slot;
                hasIdentity = true;
                identityFailure = null;
            }
            catch (Exception ex)
            {
                hasIdentity = false;
                frozenSlot = null;
                identityFailure = "capture:" + ex.GetType().Name;
            }
        }

        /// <summary>
        /// Begin a load for the same slot object captured earlier. A different
        /// object never silently reuses the frozen identity. Only current-load
        /// queued work and owned UI are cleared; the process maps are untouched.
        /// Contained.
        /// </summary>
        internal void BeginLoad(SaveSlotData slot, bool enabled)
        {
            try
            {
                this.enabled = enabled;
                CancelLoadWork();

                if (!hasIdentity)
                {
                    if (string.IsNullOrEmpty(identityFailure))
                        identityFailure = "identity-not-captured";
                    return;
                }

                if (!ReferenceEquals(slot, frozenSlot))
                {
                    hasIdentity = false;
                    frozenSlot = null;
                    identityFailure = "slot-object-mismatch";
                }
            }
            catch (Exception ex)
            {
                identityFailure = "begin-load:" + ex.GetType().Name;
            }
        }

        /// <summary>
        /// End the current load: clear its queued work and owned UI, then drop the
        /// frozen identity. The dedup and generation maps are never cleared.
        /// Contained.
        /// </summary>
        internal void EndLoad()
        {
            try
            {
                CancelLoadWork();
            }
            catch (Exception)
            {
            }

            enabled = false;
            hasIdentity = false;
            frozenSlot = null;
            identityFailure = null;
        }

        /// <summary>
        /// Advance the process slot generation for a successful native deletion so
        /// future captures get a new identity. Old-generation records are kept, so
        /// deleting the currently loaded slot cannot retrigger its own identity.
        /// Called only on an actual successful deletion. Contained.
        /// </summary>
        internal void RecordSuccessfulDeletion(string slotName, bool isDemoSave)
        {
            try
            {
                if (string.IsNullOrEmpty(slotName))
                    return;

                var key = new NativeSlotKey(slotName, isDemoSave);
                int current = generationBySlot.TryGetValue(key, out int generation) ? generation : 0;
                generationBySlot[key] = current + 1;
            }
            catch (Exception)
            {
            }
        }

        /// <summary>
        /// Evaluate the fresh snapshot against the frozen load identity and drive
        /// the popup. Contained: any unexpected failure cleans the owned partial
        /// transaction and retains the chance.
        /// </summary>
        internal SermonPopupOutcome Update(SermonStateSnapshot snapshot)
        {
            SermonPopupOutcome outcome;
            try
            {
                outcome = UpdateCore(snapshot);
            }
            catch (Exception ex)
            {
                try
                {
                    CleanupOwnedTransaction();
                }
                catch (Exception)
                {
                }

                if (hasIdentity && enabled)
                {
                    pending = true;
                    ScheduleRetry();
                }

                outcome = new SermonPopupOutcome(SermonPopupStatus.Failed, SermonPopupStage.Cleanup, ex.GetType().Name);
            }

            LastStatus = outcome.Status;
            LastStage = outcome.Stage;
            return outcome;
        }

        private SermonPopupOutcome UpdateCore(SermonStateSnapshot snapshot)
        {
            // Own-shown first: while our own modal is displayed the native pause
            // makes the safety gate false, and it must never auto-close itself.
            if (shown)
            {
                if (OwnsShownWindow())
                    return Outcome(SermonPopupStatus.Healthy, SermonPopupStage.None, "shown");

                DropOwnedReferences();
                shown = false;
            }

            if (!enabled)
            {
                CancelPendingWork();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "disabled");
            }

            if (!hasIdentity)
            {
                // Retain the identity failure so an eligible evaluation can report it.
                CancelPendingWork();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Identity, identityFailure);
            }

            if (!snapshot.Readable)
            {
                // No stale Ready/day may survive an unreadable read.
                CancelPendingWork();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "unreadable");
            }

            bool opportunity = snapshot.GatesSatisfied && snapshot.Display == SermonDisplayState.Ready;
            if (!opportunity)
            {
                // Closed gates, off day, or an already-consumed opportunity.
                CancelPendingWork();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "not-eligible");
            }

            if (dedupRecords.Contains(new DedupKey(identity, snapshot.AbsoluteDay)))
            {
                CancelPendingWork();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "already-recorded");
            }

            pending = true;

            if (!TryIsSafeToShow(out string gateReason))
                return Outcome(SermonPopupStatus.Pending, SermonPopupStage.None, gateReason);

            if (!RetryElapsed())
                return Outcome(SermonPopupStatus.Pending, SermonPopupStage.None, "retry-backoff");

            return AttemptDisplay(snapshot);
        }

        private SermonPopupOutcome AttemptDisplay(SermonStateSnapshot snapshot)
        {
            UIDialogWindow window;
            try
            {
                window = LazyUI.GetWindow<UIDialogWindow>();
            }
            catch (Exception ex)
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "window:" + ex.GetType().Name);
            }

            if (window == null)
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "window-null");
            }

            FieldInfo dataField = DialogDataField;
            if (dataField == null)
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "data-field");
            }

            // Missing localized text fails closed; the deferred catalog additions
            // are never replaced with invented copy.
            string title = SermonReminderLocalization.Get(TitleKey);
            string body = SermonReminderLocalization.Get(BodyKey);
            string confirm = SermonReminderLocalization.Get(ConfirmKey);
            if (string.IsNullOrEmpty(title) || string.IsNullOrEmpty(body) || string.IsNullOrEmpty(confirm))
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Localization, "missing-popup-text");
            }

            UIDialogWindowData data;
            try
            {
                var button = new UIDialogWindowData.ButtonData(OnConfirmPressed, confirm, null, true, GameKey.Select);
                data = new UIDialogWindowData(title, body, button);
            }
            catch (Exception ex)
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "build-data:" + ex.GetType().Name);
            }

            // Claim the transaction by installing our own data before the native
            // open, so a partial open is attributable to this attempt.
            try
            {
                dataField.SetValue(window, data);
                ownedWindow = window;
                ownedData = data;
                ownedCallback = OnNativeClosed;
            }
            catch (Exception ex)
            {
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "assign-data:" + ex.GetType().Name);
            }

            try
            {
                window.Open(data, ownedCallback);
            }
            catch (Exception ex)
            {
                CleanupOwnedTransaction();
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Open, ex.GetType().Name);
            }

            bool displayed;
            try
            {
                displayed = VerifyDisplayed(window, data, title, body, confirm);
            }
            catch (Exception ex)
            {
                CleanupOwnedTransaction();
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Show, "verify:" + ex.GetType().Name);
            }

            if (!displayed)
            {
                CleanupOwnedTransaction();
                ScheduleRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Show, "not-displayed");
            }

            // Only an actually displayed dialog records the day. Identity is the
            // frozen load identity; the day is the fresh snapshot day.
            try
            {
                dedupRecords.Add(new DedupKey(identity, snapshot.AbsoluteDay));
            }
            catch (Exception)
            {
            }

            shown = true;
            pending = false;
            retryScheduled = false;
            retryNotBeforeUnscaled = 0f;
            return Outcome(SermonPopupStatus.Healthy, SermonPopupStage.None, "displayed");
        }

        private bool VerifyDisplayed(UIDialogWindow window, UIDialogWindowData data, string title, string body, string confirm)
        {
            if (window == null || !window.IsShown)
                return false;

            // The shared data slot must still be ours.
            if (!ReferenceEquals(TryGetData(window), data))
                return false;

            // Native Close must be able to reach our callback.
            if (OnClosedField != null)
            {
                var installed = OnClosedField.GetValue(window) as Action<UIDialogWindowData>;
                if (!ReferenceEquals(installed, ownedCallback))
                    return false;
            }

            if (!ReferenceEquals(LazyWindowsStackController.ActiveWindow, window))
                return false;

            var header = HeaderField?.GetValue(window) as TextMeshProUGUI;
            var information = InformationField?.GetValue(window) as TextMeshProUGUI;
            if (header == null || information == null)
                return false;
            if (!string.Equals(header.text, title, StringComparison.Ordinal))
                return false;
            if (!string.Equals(information.text, body, StringComparison.Ordinal))
                return false;

            var activeButtons = ActiveButtonsField?.GetValue(window) as List<UIDialogWindowButton>;
            if (activeButtons == null || activeButtons.Count != 1)
                return false;

            UIDialogWindowButton button = activeButtons[0];
            if (button == null)
                return false;

            var label = ButtonLabelField?.GetValue(button) as TextMeshProUGUI;
            if (label == null)
                return false;
            if (!string.Equals(label.text, confirm, StringComparison.Ordinal))
                return false;

            return true;
        }

        private void OnConfirmPressed()
        {
            try
            {
                UIDialogWindow window = ownedWindow;
                if (window == null || !ReferenceEquals(TryGetData(window), ownedData))
                    return;

                window.Close();
            }
            catch (Exception)
            {
            }
        }

        private void OnNativeClosed(UIDialogWindowData closedData)
        {
            try
            {
                UIDialogWindow window = ownedWindow;
                UIDialogWindowData data = ownedData;
                bool ours = data != null && ReferenceEquals(closedData, data);

                shown = false;
                pending = false;
                ownedCallback = null;
                ownedWindow = null;
                ownedData = null;

                if (!ours)
                    return;

                // Our own managed callbacks always come off; rendered content and
                // the window itself only while its current data is still ours.
                ClearManagedCallbacks(data);

                try
                {
                    if (window == null || !ReferenceEquals(TryGetData(window), data))
                        return;

                    ClearRenderedContent(window);
                    ReleaseOwnedButtons(window);

                    if (DialogDataField != null && ReferenceEquals(DialogDataField.GetValue(window), data))
                        DialogDataField.SetValue(window, null);
                }
                catch (Exception)
                {
                }
            }
            catch (Exception)
            {
                shown = false;
                DropOwnedReferences();
            }
        }

        /// <summary>
        /// Whether our own data is still the window's current data and the window
        /// is actually shown. Used to keep our own pause from being read as a
        /// deferral by the safety gate.
        /// </summary>
        private bool OwnsShownWindow()
        {
            UIDialogWindow window = ownedWindow;
            if (window == null)
                return false;

            try
            {
                return window.IsShown && ReferenceEquals(TryGetData(window), ownedData);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool TryIsSafeToShow(out string reason)
        {
            reason = null;

            try
            {
                MainGame game = MainGame.Instance;
                if (game == null)
                {
                    reason = "no-main-game";
                    return false;
                }

                if (game.gameState != MainGame.GameState.InGame)
                {
                    reason = "not-in-game";
                    return false;
                }

                UILoadingOverlay overlay = LazyUI.Get<UILoadingOverlay>();
                if (overlay != null && overlay.IsShown)
                {
                    reason = "loading-overlay";
                    return false;
                }

                if (MainGame.IsGamePaused)
                {
                    reason = "paused";
                    return false;
                }

                if (LazyWindowsStackController.ActiveWindow != null)
                {
                    reason = "active-window";
                    return false;
                }

                if (LazyWindowsStackController.HasAnyModalWindowOpened)
                {
                    reason = "modal-open";
                    return false;
                }

                PlayerController player = MainGame.PlayerController;
                if (player == null)
                {
                    reason = "no-player";
                    return false;
                }

                if (!player.IsControlsEnabled)
                {
                    reason = "controls-disabled";
                    return false;
                }
            }
            catch (Exception ex)
            {
                // Fail closed when the safety state cannot be established.
                reason = "safety:" + ex.GetType().Name;
                return false;
            }

            return true;
        }

        /// <summary>
        /// Clear only current-load queued work and owned UI, closing our own
        /// modal so a load boundary cannot leave a stuck pause. Never touches the
        /// dedup or generation maps.
        /// </summary>
        private void CancelLoadWork()
        {
            CancelPendingWork();
            shown = false;
            CleanupOwnedTransaction();
        }

        /// <summary>Cancel invalid pending work only; a shown dialog is left until confirmed.</summary>
        private void CancelPendingWork()
        {
            pending = false;
            retryScheduled = false;
            retryNotBeforeUnscaled = 0f;
        }

        private void ScheduleRetry()
        {
            pending = true;
            retryScheduled = true;
            try
            {
                retryNotBeforeUnscaled = Time.unscaledTime + RetryDelaySeconds;
            }
            catch (Exception)
            {
                // Cannot measure unscaled time: do not hammer the native UI.
                retryNotBeforeUnscaled = float.PositiveInfinity;
            }
        }

        private bool RetryElapsed()
        {
            if (!retryScheduled)
                return true;

            try
            {
                if (Time.unscaledTime >= retryNotBeforeUnscaled)
                {
                    retryScheduled = false;
                    return true;
                }
            }
            catch (Exception)
            {
                return false;
            }

            return false;
        }

        /// <summary>
        /// Clean a partially or fully owned transaction. Only while the window's
        /// current data is reference-equal to our own data may its content be
        /// cleared and the window removed from the native stack. Another system's
        /// taken-over data/window is never touched.
        /// </summary>
        private void CleanupOwnedTransaction()
        {
            UIDialogWindow window = ownedWindow;
            UIDialogWindowData data = ownedData;

            ownedWindow = null;
            ownedData = null;
            ownedCallback = null;

            if (data == null)
                return;

            // Our managed data callbacks are always cleared; the window's content
            // and stack membership only while its current data is still ours, so
            // another system's taken-over window is never closed or mutated.
            ClearManagedCallbacks(data);

            if (window == null)
                return;

            try
            {
                if (!ReferenceEquals(TryGetData(window), data))
                    return;

                ClearRenderedContent(window);
                ReleaseOwnedButtons(window);

                // Remove this window from the stack without invoking any callback,
                // preserving any other modal/pause that native tracks.
                if (window.IsShown)
                    window.CloseWithoutCallback();

                // Detach our own data slot when it is still ours.
                if (DialogDataField != null && ReferenceEquals(DialogDataField.GetValue(window), data))
                    DialogDataField.SetValue(window, null);
            }
            catch (Exception)
            {
            }
        }

        private static void ClearManagedCallbacks(UIDialogWindowData data)
        {
            try
            {
                List<UIDialogWindowData.ButtonData> buttons = data.ButtonsData;
                if (buttons != null)
                {
                    foreach (UIDialogWindowData.ButtonData button in buttons)
                    {
                        if (button != null)
                            button.onPressed = null;
                    }
                }

                data.CloseButtonAction = null;
            }
            catch (Exception)
            {
            }
        }

        private void ClearRenderedContent(UIDialogWindow window)
        {
            // Actual rendered TMP text, not merely local variables.
            try
            {
                var header = HeaderField?.GetValue(window) as TextMeshProUGUI;
                if (header != null)
                    header.text = string.Empty;
            }
            catch (Exception)
            {
            }

            try
            {
                var information = InformationField?.GetValue(window) as TextMeshProUGUI;
                if (information != null)
                    information.text = string.Empty;
            }
            catch (Exception)
            {
            }
        }

        /// <summary>
        /// Return only buttons this window actually owns: the active button list
        /// plus any attached orphan under this window's own buttonsContent whose
        /// Draw failed before the native list insert. The original prefab and
        /// entries already back in the native pool are excluded. No pooled button
        /// is ever destroyed and another window's content is never scanned.
        /// </summary>
        private void ReleaseOwnedButtons(UIDialogWindow window)
        {
            Pool pool = null;
            try
            {
                pool = UIDialogWindow.pool;
            }
            catch (Exception)
            {
            }

            var content = ButtonsContentField?.GetValue(window) as RectTransform;
            var prefab = ButtonPrefabField?.GetValue(window) as UIDialogWindowButton;
            var active = ActiveButtonsField?.GetValue(window) as List<UIDialogWindowButton>;

            var candidates = new List<UIDialogWindowButton>();
            if (active != null)
            {
                foreach (UIDialogWindowButton button in active)
                {
                    if (button != null && !candidates.Contains(button))
                        candidates.Add(button);
                }
            }

            if (content != null)
            {
                int count = content.childCount;
                for (int i = 0; i < count; i++)
                {
                    Transform child = content.GetChild(i);
                    if (child == null)
                        continue;

                    var button = child.GetComponent<UIDialogWindowButton>();
                    if (button == null || ReferenceEquals(button, prefab) || candidates.Contains(button))
                        continue;

                    candidates.Add(button);
                }
            }

            foreach (UIDialogWindowButton button in candidates)
            {
                if (button == null || ReferenceEquals(button, prefab))
                    continue;

                ClearButton(button);

                bool inPool;
                try
                {
                    inPool = pool != null && pool.Objects != null && pool.Objects.Contains(button);
                }
                catch (Exception)
                {
                    inPool = true;
                }

                // Pool.ReleaseObject pushes before re-parenting; a throw after the
                // push leaves the button in the pool, so never return it twice.
                if (inPool)
                    continue;

                try
                {
                    pool?.ReleaseObject(button);
                }
                catch (Exception)
                {
                }
            }

            // Drop stale references so the next native open cannot reuse them.
            try
            {
                active?.Clear();
            }
            catch (Exception)
            {
            }
        }

        private void ClearButton(UIDialogWindowButton button)
        {
            try
            {
                LazyButton lazy = button.LazyButton;
                if (lazy != null && lazy.onClick != null)
                    lazy.onClick.RemoveAllListeners();
            }
            catch (Exception)
            {
            }

            try
            {
                var label = ButtonLabelField?.GetValue(button) as TextMeshProUGUI;
                if (label != null)
                    label.text = string.Empty;
            }
            catch (Exception)
            {
            }
        }

        private static object TryGetData(UIDialogWindow window)
        {
            if (window == null || DialogDataField == null)
                return null;

            try
            {
                return DialogDataField.GetValue(window);
            }
            catch (Exception)
            {
                return null;
            }
        }

        private void DropOwnedReferences()
        {
            ownedWindow = null;
            ownedData = null;
            ownedCallback = null;
        }

        private static SermonPopupOutcome Outcome(SermonPopupStatus status, SermonPopupStage stage, string detail) =>
            new SermonPopupOutcome(status, stage, detail);

        private readonly struct NativeSlotKey : IEquatable<NativeSlotKey>
        {
            private readonly string slotName;
            private readonly bool isDemoSave;

            internal NativeSlotKey(string slotName, bool isDemoSave)
            {
                this.slotName = slotName;
                this.isDemoSave = isDemoSave;
            }

            public bool Equals(NativeSlotKey other) =>
                isDemoSave == other.isDemoSave
                && string.Equals(slotName, other.slotName, StringComparison.Ordinal);

            public override bool Equals(object obj) => obj is NativeSlotKey other && Equals(other);

            public override int GetHashCode()
            {
                unchecked
                {
                    return ((slotName != null ? slotName.GetHashCode() : 0) * 397) ^ (isDemoSave ? 1 : 0);
                }
            }
        }

        private readonly struct SermonLoadIdentity : IEquatable<SermonLoadIdentity>
        {
            private readonly string slotName;
            private readonly bool isDemoSave;
            private readonly int generation;

            internal SermonLoadIdentity(string slotName, bool isDemoSave, int generation)
            {
                this.slotName = slotName;
                this.isDemoSave = isDemoSave;
                this.generation = generation;
            }

            public bool Equals(SermonLoadIdentity other) =>
                generation == other.generation
                && isDemoSave == other.isDemoSave
                && string.Equals(slotName, other.slotName, StringComparison.Ordinal);

            public override bool Equals(object obj) => obj is SermonLoadIdentity other && Equals(other);

            public override int GetHashCode()
            {
                unchecked
                {
                    int hash = slotName != null ? slotName.GetHashCode() : 0;
                    hash = (hash * 397) ^ (isDemoSave ? 1 : 0);
                    hash = (hash * 397) ^ generation;
                    return hash;
                }
            }
        }

        private readonly struct DedupKey : IEquatable<DedupKey>
        {
            private readonly SermonLoadIdentity identity;
            private readonly int absoluteDay;

            internal DedupKey(SermonLoadIdentity identity, int absoluteDay)
            {
                this.identity = identity;
                this.absoluteDay = absoluteDay;
            }

            public bool Equals(DedupKey other) =>
                absoluteDay == other.absoluteDay && identity.Equals(other.identity);

            public override bool Equals(object obj) => obj is DedupKey other && Equals(other);

            public override int GetHashCode()
            {
                unchecked
                {
                    return (identity.GetHashCode() * 397) ^ absoluteDay;
                }
            }
        }
    }
}
