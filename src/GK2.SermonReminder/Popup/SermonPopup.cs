using System;
using System.Collections.Generic;
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
    /// our own <see cref="UIDialogWindowData"/> before the native <c>Open</c>
    /// runs. Every native open is treated as a transaction that may fail
    /// part-way, so an <see cref="OwnedTransaction"/> retains the exact window,
    /// data, callbacks and the frozen native pool/list/content/prefab until
    /// required cleanup actually succeeds. A read failure is never treated as a
    /// confirmed takeover: only a readable, non-null, different data reference
    /// ends our ownership, and content/buttons are touched only while the
    /// window's current data is still reference-equal to ours. The cached window
    /// is never destroyed, cloned or re-initialised, and no native pool object is
    /// ever destroyed.
    ///
    /// De-duplication identity (slot name, demo flag, process slot generation) is
    /// frozen once per load in <see cref="CaptureLoadIdentity"/>; the concrete
    /// game day is read fresh from every <see cref="Update"/> call. A record is
    /// written only after the native dialog is confirmed actually displayed, and
    /// the process-memory maps are never cleared by reload, menu round-trip,
    /// overwrite, deletion or a settings toggle.
    ///
    /// Calendar logic is intentionally not reimplemented: the validated fresh
    /// <see cref="SermonStateSnapshot"/> is consumed as-is.
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

        // Pending-display retry clock (finite, bounded). "Awaiting clock" is an
        // unarmed state used when the unscaled clock is not yet readable.
        private bool pendingRetryAwaitingClock;
        private bool pendingRetryArmed;
        private float pendingRetryDeadline;

        // Cleanup retry clock, tracked separately so ineligible frames never
        // reset a failed cleanup's backoff or discard its ownership.
        private bool cleanupRetryAwaitingClock;
        private bool cleanupRetryArmed;
        private float cleanupRetryDeadline;

        // The single owned native transaction, retained until cleanup completes.
        private OwnedTransaction transaction;

        internal SermonPopupStatus LastStatus { get; private set; }
        internal SermonPopupStage LastStage { get; private set; }

        /// <summary>True while this load has queued (not yet displayed) popup work.</summary>
        internal bool IsPending => pending;

        /// <summary>True while an owned native transaction is still being resolved.</summary>
        internal bool HasUnresolvedTransaction => transaction != null;

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
        /// object never silently reuses the frozen identity. Any unresolved owned
        /// transaction is attempted first; the process maps are untouched.
        /// Contained.
        /// </summary>
        internal void BeginLoad(SaveSlotData slot, bool enabled)
        {
            try
            {
                this.enabled = enabled;
                ClearPendingRetry();
                TryResolveTransaction();

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
        /// End the current load: cancel its queued work and identity, then attempt
        /// to resolve any owned transaction without discarding it when cleanup is
        /// still incomplete. The dedup and generation maps are never cleared.
        /// Contained.
        /// </summary>
        internal void EndLoad()
        {
            ClearPendingRetry();
            enabled = false;
            hasIdentity = false;
            frozenSlot = null;
            identityFailure = null;

            try
            {
                TryResolveTransaction();
            }
            catch (Exception)
            {
            }
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
        /// the popup. Contained: an unexpected failure never discards an owned
        /// transaction before its required native cleanup succeeds.
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
                // Retain the owned transaction; native cleanup is retried, never
                // skipped, so references are not cleared before cleanup succeeds.
                if (transaction != null)
                    transaction.CleanupNeeded = true;
                ArmCleanupRetry();
                outcome = new SermonPopupOutcome(SermonPopupStatus.Failed, SermonPopupStage.Cleanup, ex.GetType().Name);
            }

            LastStatus = outcome.Status;
            LastStage = outcome.Stage;
            return outcome;
        }

        private SermonPopupOutcome UpdateCore(SermonStateSnapshot snapshot)
        {
            // Any owned transaction is resolved before new work is considered.
            if (transaction != null)
            {
                // Own-shown must precede the external pause safety gate so our own
                // native pause can never be read as a deferral that auto-closes us.
                if (!transaction.CleanupNeeded && transaction.Displayed && OwnsShownWindow(transaction))
                    return Outcome(SermonPopupStatus.Healthy, SermonPopupStage.None, "shown");

                // No longer a live owned display: it must be cleaned, not dropped.
                transaction.CleanupNeeded = true;

                if (!CleanupRetryElapsed())
                    return Outcome(SermonPopupStatus.Pending, SermonPopupStage.Cleanup, "cleanup-backoff");

                if (!RunCleanup(transaction))
                {
                    ArmCleanupRetry();
                    return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Cleanup, "cleanup-incomplete");
                }

                transaction = null;
                DisarmCleanupRetry();
            }

            if (!enabled)
            {
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "disabled");
            }

            if (!hasIdentity)
            {
                // Retain the identity failure so an eligible evaluation can report it.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Identity, identityFailure);
            }

            if (!snapshot.Readable)
            {
                // No stale Ready/day may survive an unreadable read.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "unreadable");
            }

            bool opportunity = snapshot.GatesSatisfied && snapshot.Display == SermonDisplayState.Ready;
            if (!opportunity)
            {
                // Closed gates, off day, or an already-consumed opportunity.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "not-eligible");
            }

            if (dedupRecords.Contains(new DedupKey(identity, snapshot.AbsoluteDay)))
            {
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "already-recorded");
            }

            pending = true;

            if (!TryIsSafeToShow(out string gateReason))
                return Outcome(SermonPopupStatus.Pending, SermonPopupStage.None, gateReason);

            if (!PendingRetryElapsed())
                return Outcome(SermonPopupStatus.Pending, SermonPopupStage.None, "retry-backoff");

            return AttemptDisplay(snapshot);
        }

        private SermonPopupOutcome AttemptDisplay(SermonStateSnapshot snapshot)
        {
            if (!ReflectionReady())
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "reflection-incomplete");
            }

            UIDialogWindow window;
            try
            {
                window = LazyUI.GetWindow<UIDialogWindow>();
            }
            catch (Exception ex)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "window:" + ex.GetType().Name);
            }

            if (window == null)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "window-null");
            }

            // Missing localized text fails closed; the deferred catalog additions
            // are never replaced with invented copy.
            string title = SermonReminderLocalization.Get(TitleKey);
            string body = SermonReminderLocalization.Get(BodyKey);
            string confirm = SermonReminderLocalization.Get(ConfirmKey);
            if (string.IsNullOrEmpty(title) || string.IsNullOrEmpty(body) || string.IsNullOrEmpty(confirm))
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Localization, "missing-popup-text");
            }

            // After resolving the shared window and text, revalidate gameplay and
            // that the resolved window is not already shown before any preclaim, so
            // an in-use shared dialog is never overwritten.
            if (!TryIsSafeToShow(out string recheckReason))
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Pending, SermonPopupStage.None, recheckReason);
            }

            bool shownAlready;
            try
            {
                shownAlready = window.IsShown;
            }
            catch (Exception ex)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "is-shown:" + ex.GetType().Name);
            }

            if (shownAlready)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve, "window-in-use");
            }

            // Freeze the exact native pool/list/content/prefab before preclaim so
            // cleanup can still run after the shared window changes underneath us.
            Pool pool;
            List<UIDialogWindowButton> activeList;
            RectTransform content;
            UIDialogWindowButton prefab;
            if (!TryFreezeNativeRefs(window, out pool, out activeList, out content, out prefab))
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "freeze-refs");
            }

            var tx = new OwnedTransaction
            {
                Window = window,
                Pool = pool,
                ActiveList = activeList,
                Content = content,
                Prefab = prefab
            };
            tx.NativeClosed = closed => OnNativeClosed(tx, closed);
            tx.Confirm = () => OnConfirm(tx);

            UIDialogWindowData data;
            try
            {
                var button = new UIDialogWindowData.ButtonData(tx.Confirm, confirm, null, true, GameKey.Select);
                data = new UIDialogWindowData(title, body, button);
                data.CloseButtonAction = tx.Confirm;
            }
            catch (Exception ex)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "build-data:" + ex.GetType().Name);
            }

            tx.Data = data;

            // Claim the transaction by installing our own data before the native
            // open, so a partial open is attributable to this attempt.
            try
            {
                DialogDataField.SetValue(window, data);
            }
            catch (Exception ex)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "assign-data:" + ex.GetType().Name);
            }

            transaction = tx;

            try
            {
                window.Open(data, tx.NativeClosed);
            }
            catch (Exception ex)
            {
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Open, ex.GetType().Name);
            }

            bool displayed;
            try
            {
                displayed = VerifyDisplayed(window, data, title, body, confirm, tx.NativeClosed);
            }
            catch (Exception ex)
            {
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Show, "verify:" + ex.GetType().Name);
            }

            if (!displayed)
            {
                FailTransaction(tx);
                ArmPendingRetry();
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

            tx.Displayed = true;
            tx.CleanupNeeded = false;
            ClearPendingRetry();
            DisarmCleanupRetry();
            return Outcome(SermonPopupStatus.Healthy, SermonPopupStage.None, "displayed");
        }

        /// <summary>
        /// Mark the attempt failed and run required cleanup now; if cleanup is
        /// incomplete the transaction is retained so it blocks new opens and is
        /// retried with the finite cleanup clock.
        /// </summary>
        private void FailTransaction(OwnedTransaction tx)
        {
            tx.CleanupNeeded = true;
            if (RunCleanup(tx) && ReferenceEquals(transaction, tx))
            {
                transaction = null;
                DisarmCleanupRetry();
                return;
            }

            // Cleanup is incomplete: retain ownership and bound the next attempt.
            ArmCleanupRetry();
        }

        private bool VerifyDisplayed(
            UIDialogWindow window,
            UIDialogWindowData data,
            string title,
            string body,
            string confirm,
            Action<UIDialogWindowData> expectedCallback)
        {
            if (window == null || !window.IsShown)
                return false;

            // The shared data slot must be readable and still ours.
            if (!TryReadData(window, out object current) || !ReferenceEquals(current, data))
                return false;

            // The exact installed callback must be reachable, never skipped.
            if (OnClosedField == null)
                return false;
            var installed = OnClosedField.GetValue(window) as Action<UIDialogWindowData>;
            if (!ReferenceEquals(installed, expectedCallback))
                return false;

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

        /// <summary>Confirm/close action bound to its exact transaction.</summary>
        private void OnConfirm(OwnedTransaction tx)
        {
            try
            {
                if (!ReferenceEquals(transaction, tx) || tx.CleanupNeeded)
                    return;
                if (tx.Window == null || !ReferenceEquals(TryGetData(tx.Window), tx.Data))
                    return;

                tx.Window.Close();
            }
            catch (Exception)
            {
            }
        }

        /// <summary>
        /// Native-close callback bound to its exact transaction. It marks only its
        /// own transaction for deferred cleanup; native cleanup runs outside the
        /// in-progress native Close call, on Update/EndLoad, never reentrantly.
        /// A stale or mismatched callback never alters any newer transaction.
        /// </summary>
        private void OnNativeClosed(OwnedTransaction tx, UIDialogWindowData closedData)
        {
            try
            {
                if (!ReferenceEquals(transaction, tx))
                    return;
                if (tx.Data == null || !ReferenceEquals(tx.Data, closedData))
                    return;

                tx.Displayed = false;
                tx.CleanupNeeded = true;
            }
            catch (Exception)
            {
                tx.Displayed = false;
                tx.CleanupNeeded = true;
            }
        }

        /// <summary>
        /// Whether our own data is still the window's current data and the window
        /// is actually shown. Used to keep our own pause from being read as a
        /// deferral by the safety gate.
        /// </summary>
        private static bool OwnsShownWindow(OwnedTransaction tx)
        {
            if (tx == null || tx.Window == null || tx.Data == null)
                return false;

            try
            {
                if (!tx.Window.IsShown)
                    return false;
                return TryReadData(tx.Window, out object current) && ReferenceEquals(current, tx.Data);
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

        // --- cleanup -----------------------------------------------------------------

        /// <summary>
        /// Attempt to resolve the owned transaction, respecting the finite cleanup
        /// backoff. Retains it when cleanup is incomplete.
        /// </summary>
        private void TryResolveTransaction()
        {
            if (transaction == null)
                return;

            transaction.CleanupNeeded = true;
            if (!CleanupRetryElapsed())
                return;

            if (RunCleanup(transaction))
            {
                transaction = null;
                DisarmCleanupRetry();
            }
            else
            {
                ArmCleanupRetry();
            }
        }

        /// <summary>
        /// Resolve an owned transaction. Managed callbacks always come off. Native
        /// content, buttons, window membership and the data slot are touched only
        /// while the window's current data is readable and reference-equal to ours.
        /// A confirmed takeover only clears our managed closures and ends
        /// ownership; an unreadable read retains ownership and fails closed.
        /// Returns true only when every required step has succeeded.
        /// </summary>
        private bool RunCleanup(OwnedTransaction tx)
        {
            if (tx == null)
                return true;

            if (!tx.CallbacksCleared)
            {
                try
                {
                    ClearManagedCallbacks(tx.Data);
                    tx.CallbacksCleared = true;
                }
                catch (Exception)
                {
                    // Remains false; retried.
                }
            }

            // A confirmed ownership end (genuine destruction or foreign takeover)
            // leaves only the managed closures to finish.
            if (tx.Relinquished)
                return tx.CallbacksCleared;

            UIDialogWindow window = tx.Window;
            if (window == null)
            {
                // Genuine native destruction: nothing remains that we may clean.
                tx.Relinquished = true;
                return tx.CallbacksCleared;
            }

            if (!TryReadData(window, out object current))
                return false; // Unreadable is not a confirmed takeover.

            if (current == null)
                return false; // Ambiguous, not proven foreign; retain and retry.

            if (!ReferenceEquals(current, tx.Data))
            {
                // Confirmed takeover: never touch foreign content/buttons/window.
                tx.Relinquished = true;
                return tx.CallbacksCleared;
            }

            // Still ours: clear rendered content (best-effort, independent).
            if (!tx.ContentCleared)
            {
                bool headerOk = TryClearTmp(window, HeaderField);
                bool bodyOk = TryClearTmp(window, InformationField);
                tx.ContentCleared = headerOk && bodyOk;
            }

            // Buttons are attempted independently so visible content is suppressed
            // even if another cleanup step failed.
            ButtonProcessResult buttonResult = ProcessOwnedButtons(tx);
            if (buttonResult == ButtonProcessResult.Relinquished)
            {
                tx.Relinquished = true;
                return tx.CallbacksCleared;
            }

            tx.ButtonsResolved = buttonResult == ButtonProcessResult.Complete;

            // Recheck ownership around the callback-producing native close.
            if (!TryReadData(window, out object beforeClose))
                return false;
            if (!ReferenceEquals(beforeClose, tx.Data))
            {
                // Confirmed takeover before the close: never close foreign content.
                tx.Relinquished = true;
                return tx.CallbacksCleared;
            }

            if (!tx.WindowsClosed)
            {
                try
                {
                    // Idempotent even when IsShown is already false: a partial
                    // HideWindow (isShown set false before stack removal) is not
                    // proof of cleanup.
                    window.CloseWithoutCallback();
                    tx.WindowsClosed = true;
                }
                catch (Exception)
                {
                }
            }

            // Detach our own data claim ONLY after every prior required stage
            // succeeded; otherwise the exact claim and stage state are retained so
            // the unresolved work is reattempted.
            if (tx.ContentCleared && tx.ButtonsResolved && tx.WindowsClosed && !tx.DataDetached)
            {
                if (!TryReadData(window, out object preDetach))
                    return false;

                if (preDetach == null)
                    return false; // Ambiguous: never invented as successful cleanup.

                if (ReferenceEquals(preDetach, tx.Data))
                {
                    try
                    {
                        DialogDataField.SetValue(window, null);
                        tx.DataDetached = true;
                    }
                    catch (Exception)
                    {
                        return false;
                    }
                }
                else
                {
                    // Taken over between steps: not ours to detach.
                    tx.Relinquished = true;
                    return tx.CallbacksCleared;
                }
            }

            bool complete = tx.CallbacksCleared
                && tx.ContentCleared
                && tx.ButtonsResolved
                && tx.WindowsClosed
                && tx.DataDetached;
            if (complete)
                tx.Relinquished = true;
            return complete;
        }

        private static void ClearManagedCallbacks(UIDialogWindowData data)
        {
            if (data == null)
                return;

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

        private static bool TryClearTmp(UIDialogWindow window, FieldInfo field)
        {
            if (field == null)
                return false;

            try
            {
                var tmp = field.GetValue(window) as TextMeshProUGUI;
                if (tmp == null)
                    return false;
                tmp.text = string.Empty;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// Return only buttons this window actually owns: the union of the frozen
        /// active list and the attached children of this window's frozen
        /// buttonsContent, excluding the original prefab. Each item is tracked
        /// through Owned -> (Transferred | Uncertain) phases. Membership is checked
        /// by reference identity before any mutation, an already returned item is
        /// never touched, and an uncertain item is only re-checked read-only.
        /// </summary>
        private ButtonProcessResult ProcessOwnedButtons(OwnedTransaction tx)
        {
            // Discover the frozen candidates and ALWAYS track whatever was found,
            // so a discovery failure never hides an already-known owned item.
            bool discoverySucceeded = DiscoverOwnedButtons(tx, out List<UIDialogWindowButton> discovered);
            foreach (UIDialogWindowButton candidate in discovered)
                EnsureButtonState(tx, candidate);

            bool allDone = discoverySucceeded;

            foreach (ButtonState state in tx.Buttons.ToArray())
            {
                UIDialogWindowButton button = state.Button;

                if (button == null)
                {
                    // Genuine native destruction: this item's ownership ends.
                    state.Phase = ButtonPhase.Transferred;
                    continue;
                }

                if (state.Phase == ButtonPhase.Transferred)
                    continue;

                if (state.Phase == ButtonPhase.Uncertain)
                {
                    // Read-only membership rechecks only; never re-mutate or release.
                    if (!IsInPoolReadable(tx.Pool, button, out bool uncertainInPool))
                    {
                        allDone = false;
                        continue;
                    }

                    if (uncertainInPool)
                        state.Phase = ButtonPhase.Transferred;
                    else
                        allDone = false;
                    continue;
                }

                // Owned: membership must be readable before we touch the item.
                if (!IsInPoolReadable(tx.Pool, button, out bool inPool))
                {
                    allDone = false;
                    continue;
                }

                if (inPool)
                {
                    // Already returned by someone: never clear or return again.
                    state.Phase = ButtonPhase.Transferred;
                    continue;
                }

                // Revalidate exact window-data ownership immediately before any
                // mutable step: an earlier release may have let another system take
                // over the same shared window and reuse these buttons. A cached
                // candidate array never grants permission to touch reused content.
                ButtonOwner owner = ClassifyOwnership(tx);
                if (owner == ButtonOwner.Unreadable)
                {
                    allDone = false;
                    continue;
                }

                if (owner == ButtonOwner.Foreign)
                    return ButtonProcessResult.Relinquished;

                if (!state.Cleared)
                    state.Cleared = TryClearButton(button);

                if (!state.Hidden)
                    state.Hidden = TryHideButton(button);

                // Re-check ownership after the callback-producing hide.
                owner = ClassifyOwnership(tx);
                if (owner == ButtonOwner.Unreadable)
                {
                    allDone = false;
                    continue;
                }

                if (owner == ButtonOwner.Foreign)
                    return ButtonProcessResult.Relinquished;

                if (!state.ListRemoved)
                    state.ListRemoved = RemoveFromActiveList(tx, button);

                // Required actual clearing, hiding AND verified exact-reference
                // removal from the original active list must succeed first.
                if (!state.Cleared || !state.Hidden || !state.ListRemoved)
                {
                    allDone = false;
                    continue;
                }

                if (tx.Pool == null)
                {
                    // Absence of a pool is NOT evidence of return: retain.
                    allDone = false;
                    continue;
                }

                try
                {
                    // Nonthrowing ReleaseObject transfers ownership (it pushes first).
                    tx.Pool.ReleaseObject(button);
                    state.Phase = ButtonPhase.Transferred;
                }
                catch (Exception)
                {
                    if (IsInPoolReadable(tx.Pool, button, out bool afterThrow) && afterThrow)
                        state.Phase = ButtonPhase.Transferred;
                    else
                        state.Phase = ButtonPhase.Uncertain;
                }

                if (state.Phase != ButtonPhase.Transferred)
                    allDone = false;
            }

            return allDone ? ButtonProcessResult.Complete : ButtonProcessResult.Incomplete;
        }

        private static bool DiscoverOwnedButtons(OwnedTransaction tx, out List<UIDialogWindowButton> discovered)
        {
            discovered = new List<UIDialogWindowButton>();
            bool success = true;

            List<UIDialogWindowButton> activeList = tx.ActiveList;
            if (activeList == null)
            {
                success = false;
            }
            else
            {
                try
                {
                    foreach (UIDialogWindowButton button in activeList)
                    {
                        if (button != null && !ReferenceEquals(button, tx.Prefab) && !ContainsByIdentity(discovered, button))
                            discovered.Add(button);
                    }
                }
                catch (Exception)
                {
                    success = false;
                }
            }

            RectTransform content = tx.Content;
            if (content == null)
            {
                success = false;
            }
            else
            {
                int count;
                try
                {
                    count = content.childCount;
                }
                catch (Exception)
                {
                    count = -1;
                }

                if (count < 0)
                {
                    // Unknown orphan set: discovery did not complete.
                    success = false;
                }
                else
                {
                    for (int i = 0; i < count; i++)
                    {
                        Transform child;
                        try
                        {
                            child = content.GetChild(i);
                        }
                        catch (Exception)
                        {
                            success = false;
                            continue;
                        }

                        if (child == null)
                        {
                            // Genuine destroyed child may end its ownership; an
                            // unreadable live component below is not destroyed.
                            continue;
                        }

                        UIDialogWindowButton button;
                        try
                        {
                            button = child.GetComponent<UIDialogWindowButton>();
                        }
                        catch (Exception)
                        {
                            success = false;
                            continue;
                        }

                        if (button == null || ReferenceEquals(button, tx.Prefab) || ContainsByIdentity(discovered, button))
                            continue;

                        discovered.Add(button);
                    }
                }
            }

            return success;
        }

        private static bool ContainsByIdentity(List<UIDialogWindowButton> list, UIDialogWindowButton button)
        {
            foreach (UIDialogWindowButton existing in list)
            {
                if (ReferenceEquals(existing, button))
                    return true;
            }

            return false;
        }

        private static ButtonOwner ClassifyOwnership(OwnedTransaction tx)
        {
            if (tx == null || tx.Window == null || tx.Data == null)
                return ButtonOwner.Unreadable;

            if (!TryReadData(tx.Window, out object current))
                return ButtonOwner.Unreadable;

            if (current == null)
                return ButtonOwner.Unreadable;

            return ReferenceEquals(current, tx.Data) ? ButtonOwner.Owned : ButtonOwner.Foreign;
        }

        private static void EnsureButtonState(OwnedTransaction tx, UIDialogWindowButton button)
        {
            foreach (ButtonState state in tx.Buttons)
            {
                if (ReferenceEquals(state.Button, button))
                    return;
            }

            tx.Buttons.Add(new ButtonState { Button = button });
        }

        private static bool RemoveFromActiveList(OwnedTransaction tx, UIDialogWindowButton button)
        {
            if (tx.ActiveList == null)
                return false;

            try
            {
                return RemoveByIdentity(tx.ActiveList, button);
            }
            catch (Exception)
            {
                // A failed removal must not be treated as success.
                return false;
            }
        }

        private static bool RemoveByIdentity(List<UIDialogWindowButton> list, UIDialogWindowButton button)
        {
            for (int i = 0; i < list.Count; i++)
            {
                if (ReferenceEquals(list[i], button))
                {
                    list.RemoveAt(i);
                    return true;
                }
            }

            return false;
        }

        private static bool TryClearButton(UIDialogWindowButton button)
        {
            if (button == null)
                return true;

            bool ok = true;

            try
            {
                LazyButton lazy = button.LazyButton;
                if (lazy != null && lazy.onClick != null)
                    lazy.onClick.RemoveAllListeners();
            }
            catch (Exception)
            {
                ok = false;
            }

            try
            {
                var label = ButtonLabelField?.GetValue(button) as TextMeshProUGUI;
                if (label != null)
                    label.text = string.Empty;
            }
            catch (Exception)
            {
                ok = false;
            }

            return ok;
        }

        private static bool TryHideButton(UIDialogWindowButton button)
        {
            try
            {
                if (button == null || button.gameObject == null)
                    return true;
                button.gameObject.SetActive(false);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static bool IsInPoolReadable(Pool pool, UIDialogWindowButton button, out bool inPool)
        {
            inPool = false;

            // No pool means nothing can have been returned through it.
            if (pool == null)
                return true;

            try
            {
                Stack<MonoBehaviour> objects = pool.Objects;
                if (objects == null)
                    return false;

                // Reference identity, not Stack.Contains default Unity equality.
                foreach (MonoBehaviour item in objects)
                {
                    if (ReferenceEquals(item, button))
                    {
                        inPool = true;
                        break;
                    }
                }

                return true;
            }
            catch (Exception)
            {
                // Unreadable membership does not prove returned.
                return false;
            }
        }

        private static bool TryFreezeNativeRefs(
            UIDialogWindow window,
            out Pool pool,
            out List<UIDialogWindowButton> activeList,
            out RectTransform content,
            out UIDialogWindowButton prefab)
        {
            pool = null;
            activeList = null;
            content = null;
            prefab = null;

            try
            {
                pool = UIDialogWindow.pool;
                activeList = ActiveButtonsField?.GetValue(window) as List<UIDialogWindowButton>;
                content = ButtonsContentField?.GetValue(window) as RectTransform;
                prefab = ButtonPrefabField?.GetValue(window) as UIDialogWindowButton;
            }
            catch (Exception)
            {
                return false;
            }

            return pool != null && activeList != null && content != null && prefab != null;
        }

        private static bool ReflectionReady() =>
            DialogDataField != null
            && OnClosedField != null
            && HeaderField != null
            && InformationField != null
            && ButtonsContentField != null
            && ActiveButtonsField != null
            && ButtonPrefabField != null
            && ButtonLabelField != null;

        private static bool TryReadData(UIDialogWindow window, out object value)
        {
            value = null;
            if (window == null || DialogDataField == null)
                return false;

            try
            {
                value = DialogDataField.GetValue(window);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static object TryGetData(UIDialogWindow window)
        {
            TryReadData(window, out object value);
            return value;
        }

        // --- retry clocks ------------------------------------------------------------

        private void ArmPendingRetry()
        {
            pending = true;
            pendingRetryArmed = false;
            pendingRetryAwaitingClock = true;
            pendingRetryDeadline = 0f;
        }

        private void ClearPendingRetry()
        {
            pending = false;
            pendingRetryArmed = false;
            pendingRetryAwaitingClock = false;
            pendingRetryDeadline = 0f;
        }

        private bool PendingRetryElapsed()
        {
            if (!pending && !pendingRetryArmed && !pendingRetryAwaitingClock)
                return true;

            if (pendingRetryAwaitingClock)
            {
                if (!TryReadClock(out float armedAt))
                    return false; // Wait for a finite clock; never arm on Infinity.
                pendingRetryAwaitingClock = false;
                pendingRetryArmed = true;
                pendingRetryDeadline = armedAt + RetryDelaySeconds;
            }

            if (!pendingRetryArmed)
                return true;

            if (!TryReadClock(out float now))
                return false;

            if (now >= pendingRetryDeadline)
            {
                pendingRetryArmed = false;
                return true;
            }

            return false;
        }

        private void ArmCleanupRetry()
        {
            cleanupRetryArmed = false;
            cleanupRetryAwaitingClock = true;
            cleanupRetryDeadline = 0f;
        }

        private void DisarmCleanupRetry()
        {
            cleanupRetryArmed = false;
            cleanupRetryAwaitingClock = false;
            cleanupRetryDeadline = 0f;
        }

        private bool CleanupRetryElapsed()
        {
            if (cleanupRetryAwaitingClock)
            {
                if (!TryReadClock(out float armedAt))
                    return false;
                cleanupRetryAwaitingClock = false;
                cleanupRetryArmed = true;
                cleanupRetryDeadline = armedAt + RetryDelaySeconds;
            }

            if (!cleanupRetryArmed)
                return true;

            if (!TryReadClock(out float now))
                return false;

            if (now >= cleanupRetryDeadline)
            {
                cleanupRetryArmed = false;
                return true;
            }

            return false;
        }

        private static bool TryReadClock(out float now)
        {
            now = 0f;

            try
            {
                now = Time.unscaledTime;
            }
            catch (Exception)
            {
                return false;
            }

            if (float.IsNaN(now) || float.IsInfinity(now))
                return false;

            return true;
        }

        private static SermonPopupOutcome Outcome(SermonPopupStatus status, SermonPopupStage stage, string detail) =>
            new SermonPopupOutcome(status, stage, detail);

        // --- nested state ------------------------------------------------------------

        private enum ButtonPhase
        {
            Owned,
            Uncertain,
            Transferred
        }

        private enum ButtonOwner
        {
            Owned,
            Foreign,
            Unreadable
        }

        private enum ButtonProcessResult
        {
            Complete,
            Incomplete,
            Relinquished
        }

        private sealed class ButtonState
        {
            internal UIDialogWindowButton Button;
            internal ButtonPhase Phase;
            internal bool Cleared;
            internal bool Hidden;
            internal bool ListRemoved;
        }

        /// <summary>
        /// One owned native open transaction. Retains the exact window, data,
        /// callbacks and frozen native pool/list/content/prefab plus per-button
        /// return phases until every required cleanup step succeeds.
        /// </summary>
        private sealed class OwnedTransaction
        {
            internal UIDialogWindow Window;
            internal UIDialogWindowData Data;
            internal Action<UIDialogWindowData> NativeClosed;
            internal Action Confirm;

            internal Pool Pool;
            internal List<UIDialogWindowButton> ActiveList;
            internal RectTransform Content;
            internal UIDialogWindowButton Prefab;

            internal bool Displayed;
            internal bool CleanupNeeded;
            internal bool CallbacksCleared;
            internal bool ContentCleared;
            internal bool ButtonsResolved;
            internal bool WindowsClosed;
            internal bool DataDetached;

            // A confirmed foreign takeover or genuine destruction is a distinct,
            // allowed ownership-end path that leaves only management cleanup.
            internal bool Relinquished;

            internal readonly List<ButtonState> Buttons = new List<ButtonState>();
        }

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
