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
        private const string AffirmativeLabelKey = "gksr.popup.sermon.confirm";

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

        // Current failure evidence, independent of any logging or notice budget.
        // `attemptedDisplayFaulted` records a concrete failed evaluation/attempt;
        // `cleanupFaulted` records required owned cleanup that is still unresolved.
        // They are kept distinct because a genuine load boundary discards only the
        // ordinary attempted-display evidence, while an unresolved owned cleanup must
        // stay reported until it actually resolves.
        private bool attemptedDisplayFaulted;
        private bool cleanupFaulted;

        internal SermonPopupStatus LastStatus { get; private set; }
        internal SermonPopupStage LastStage { get; private set; }

        /// <summary>True while this load has queued (not yet displayed) popup work.</summary>
        internal bool IsPending => pending;

        /// <summary>True while an owned native transaction is still being resolved.</summary>
        internal bool HasUnresolvedTransaction => transaction != null;

        /// <summary>
        /// True while a concrete popup fault is active: a Failed evaluation/attempt is
        /// recorded, or a required owned cleanup is still unresolved. Preserved across
        /// ordinary Pending and bounded backoff; cleared on an actual Healthy
        /// completion, a genuinely Inactive boundary, or the moment an existing owned
        /// cleanup resolves. It never depends on logging or notice budgets.
        /// </summary>
        internal bool HasActiveFailure => attemptedDisplayFaulted || cleanupFaulted;

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

                // A genuine load boundary discards the prior load's ordinary
                // attempted-display evidence; an unresolved owned cleanup keeps its own
                // evidence until TryResolveTransaction actually resolves it.
                attemptedDisplayFaulted = false;
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

            // A genuine lifecycle boundary discards ordinary attempted-display
            // evidence; an unresolved owned cleanup keeps its own evidence until it
            // resolves.
            attemptedDisplayFaulted = false;

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
        /// Bind the plugin's existing Harmony owner for the lazy provenance install.
        /// Contained; when the observer stays unavailable the popup fails closed
        /// before any pre-claim or native open.
        /// </summary>
        internal void ConfigureHarmony(Harmony harmony)
        {
            try { SermonPopupButtonCapture.ConfigureHarmony(harmony); }
            catch (Exception) { }
        }

        /// <summary>
        /// Drop the provenance configuration on teardown. The owner's UnpatchSelf
        /// already removed this transpiler; this only clears the cached owner and the
        /// supported flag so a re-enable cannot trust a stale success.
        /// </summary>
        internal void ClearHarmonyConfiguration()
        {
            try { SermonPopupButtonCapture.ClearConfiguration(); }
            catch (Exception) { }
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
                // skipped, so references are not cleared before cleanup succeeds. The
                // Failed(Cleanup) outcome below derives the failure evidence.
                if (transaction != null)
                    transaction.CleanupNeeded = true;
                ArmCleanupRetry();
                outcome = new SermonPopupOutcome(SermonPopupStatus.Failed, SermonPopupStage.Cleanup, ex.GetType().Name);
            }

            // Derive the current failure episode from the concrete outcome, not from
            // any diagnostic text or notice budget.
            UpdateFailureEvidence(outcome);

            LastStatus = outcome.Status;
            LastStage = outcome.Stage;
            return outcome;
        }

        /// <summary>
        /// Fold one concrete outcome into the popup failure evidence. A Healthy or
        /// genuinely Inactive outcome ends the ordinary attempted-display episode; a
        /// concrete Failed outcome starts it; a Pending hold is never recovery. A
        /// still-unresolved owned cleanup is tracked separately and ended only at its
        /// own resolution boundary.
        /// </summary>
        private void UpdateFailureEvidence(SermonPopupOutcome outcome)
        {
            switch (outcome.Status)
            {
                case SermonPopupStatus.Healthy:
                    // An actual displayed or live-shown popup is recovery.
                    attemptedDisplayFaulted = false;
                    cleanupFaulted = false;
                    break;

                case SermonPopupStatus.Failed:
                    attemptedDisplayFaulted = true;
                    if (outcome.Stage == SermonPopupStage.Cleanup)
                        cleanupFaulted = true;
                    break;

                case SermonPopupStatus.Inactive:
                    // Genuinely Inactive (disabled, not eligible, unreadable, no work)
                    // ends ordinary attempted-display evidence. The popup core never
                    // returns Inactive while an owned transaction is unresolved, so a
                    // still-pending cleanup keeps its own evidence.
                    attemptedDisplayFaulted = false;
                    break;

                default:
                    // Pending (unsafe UI, gate held, or bounded backoff) preserves the
                    // episode: it is not recovery.
                    break;
            }
        }

        /// <summary>
        /// The explicit recovery boundary of a failed transaction: once its required
        /// owned cleanup has completed, both the cleanup evidence and the ordinary
        /// attempted-display evidence for that transaction end immediately, even when
        /// the same Update then returns an ordinary Pending due to an external gate.
        /// </summary>
        private void ClearFailedTransactionEvidence()
        {
            cleanupFaulted = false;
            attemptedDisplayFaulted = false;
        }

        private SermonPopupOutcome UpdateCore(SermonStateSnapshot snapshot)
        {
            // The fresh readability fact is needed before the owned-shown shortcut: an
            // unreadable shared state must close/suppress our own dialog, never report
            // Healthy.
            bool readable = snapshot.Readable;

            // Any owned transaction is resolved before new work is considered.
            if (transaction != null)
            {
                // Own-shown must precede the external pause safety gate so our own
                // native pause can never be read as a deferral that auto-closes us. It
                // is only a live healthy display while the fresh state is readable.
                if (readable && !transaction.CleanupNeeded && transaction.Displayed && OwnsShownWindow(transaction))
                    return Outcome(SermonPopupStatus.Healthy, SermonPopupStage.None, "shown");

                // No longer a live owned display (or the shared state is unreadable):
                // it must be cleaned, not dropped. A shared-state failure never calls
                // EndLoad/BeginLoad, never recaptures identity, and never clears the
                // process dedup maps.
                transaction.CleanupNeeded = true;

                if (!CleanupRetryElapsed())
                    return Outcome(SermonPopupStatus.Pending, SermonPopupStage.Cleanup, "cleanup-backoff");

                if (!RunCleanup(transaction))
                {
                    ArmCleanupRetry();
                    return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Cleanup, "cleanup-incomplete");
                }

                // Required owned cleanup completed: this is the explicit recovery
                // boundary for the failed-transaction/cleanup episode. It ends here,
                // before any later Pending gate, so a repaired cleanup never leaves a
                // stale fault notice queued and never opens a replacement.
                transaction = null;
                DisarmCleanupRetry();
                ClearFailedTransactionEvidence();
            }

            if (!enabled)
            {
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "disabled");
            }

            if (!readable)
            {
                // An unreadable shared state suppresses dependent popup eligibility. It
                // is never classified as an identity fault, so unreadability alone does
                // not invent a popup identity failure. No stale Ready/day survives it.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "unreadable");
            }

            bool opportunity = snapshot.GatesSatisfied && snapshot.Display == SermonDisplayState.Ready;
            if (!opportunity)
            {
                // Closed gates, off day, or an already-consumed opportunity: not
                // eligible, so a missing identity is never implicated or reported.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Inactive, SermonPopupStage.None, "not-eligible");
            }

            if (!hasIdentity)
            {
                // Identity is required only for the dedup classification of an eligible
                // opportunity; retain the failure so a readable eligible evaluation
                // reports it.
                ClearPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Identity, identityFailure);
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
            string confirm = SermonReminderLocalization.Get(AffirmativeLabelKey);
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

            // Provenance capability is required BEFORE any pre-claim or native open:
            // without an instrumented acquisition observer the mod cannot prove which
            // buttons this attempt acquired, so it must fail closed instead of
            // claiming the frozen active list. Lazy install is bounded to this attempt
            // (AttemptDisplay already runs on the finite retry clock).
            if (!SermonPopupButtonCapture.EnsureReady(out string captureDetail))
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Resolve,
                    "capture:" + (captureDetail ?? "unavailable"));
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
            tx.AffirmativeAction = () => OnAffirmativeAction(tx);

            UIDialogWindowData data;
            try
            {
                var button = new UIDialogWindowData.ButtonData(tx.AffirmativeAction, confirm, null, true, GameKey.Select);
                data = new UIDialogWindowData(title, body, button);
                data.CloseButtonAction = tx.AffirmativeAction;
            }
            catch (Exception ex)
            {
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "build-data:" + ex.GetType().Name);
            }

            tx.Data = data;
            tx.ExpectedAcquisitionCount = data.ButtonsData != null ? data.ButtonsData.Count : 0;

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

            SermonPopupButtonCapture.CaptureScope scope = null;
            try
            {
                scope = SermonPopupButtonCapture.BeginScope(window, data, button => RecordAcquiredButton(tx, button));
            }
            catch (Exception ex)
            {
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "capture-scope:" + ex.GetType().Name);
            }

            if (scope == null)
            {
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Claim, "capture-scope-null");
            }

            // Run the native open inside the observation scope, then finalize the
            // capture outcome and restore the scope BEFORE any native cleanup.
            // FailTransaction must never run while the observation scope is still
            // active or before a lost record is reflected on the transaction.
            Exception openException = null;
            try
            {
                window.Open(data, tx.NativeClosed);
            }
            catch (Exception ex)
            {
                openException = ex;
            }
            finally
            {
                // A failure to read the scope outcome or to unwind the scope is itself
                // a loss of provenance and must fail closed, never read as success.
                try { tx.CaptureRecordFailed = tx.CaptureRecordFailed || scope.RecordFailed; }
                catch (Exception) { tx.CaptureRecordFailed = true; }

                try { scope.Dispose(); }
                catch (Exception) { tx.CaptureRecordFailed = true; }
            }

            if (openException != null)
            {
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Open, openException.GetType().Name);
            }

            // A normal return without the complete expected acquisition set cannot be
            // verified or de-duplicated. Mark the provenance permanently incomplete
            // BEFORE cleanup so the retained transaction can never report completion,
            // then fail closed instead of recording a day the native dialog may not
            // actually own.
            if (!tx.CaptureExpectedMet)
            {
                tx.CaptureRecordFailed = true;
                FailTransaction(tx);
                ArmPendingRetry();
                return Outcome(SermonPopupStatus.Failed, SermonPopupStage.Show, "capture-incomplete");
            }

            bool displayed;
            try
            {
                displayed = VerifyDisplayed(window, data, title, body, confirm, tx.NativeClosed, tx);
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

            // Cleanup is incomplete: retain ownership and bound the next attempt. The
            // unresolved owned cleanup is its own failure evidence until it resolves.
            ArmCleanupRetry();
            cleanupFaulted = true;
        }

        private bool VerifyDisplayed(
            UIDialogWindow window,
            UIDialogWindowData data,
            string title,
            string body,
            string confirm,
            Action<UIDialogWindowData> expectedCallback,
            OwnedTransaction tx)
        {
            if (window == null || !window.IsShown)
                return false;

            // Incomplete capture must fail closed: a normal return without the exact
            // expected acquisition set is never verifiable or de-duplicated.
            if (tx == null || !tx.CaptureExpectedMet)
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

            // The sole active button must be exactly the reference the native Open
            // acquired for this attempt, never an unrelated lookalike.
            if (tx.Buttons.Count != 1 || !ReferenceEquals(button, tx.Buttons[0].Button))
                return false;

            var label = ButtonLabelField?.GetValue(button) as TextMeshProUGUI;
            if (label == null)
                return false;
            if (!string.Equals(label.text, confirm, StringComparison.Ordinal))
                return false;

            return true;
        }

        /// <summary>Affirmative/close action bound to its exact transaction.</summary>
        private void OnAffirmativeAction(OwnedTransaction tx)
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
                ClearFailedTransactionEvidence();
            }
            else
            {
                ArmCleanupRetry();
                cleanupFaulted = true;
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
                // Verified takeover: never touch foreign content/buttons/window.
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
            if (beforeClose == null)
            {
                // A readable null is ambiguous, not a confirmed takeover: retain
                // and defer rather than relinquishing our claim.
                return false;
            }
            if (!ReferenceEquals(beforeClose, tx.Data))
            {
                // Verified nonnull foreign content: permitted takeover end.
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

            // Detach our own data claim ONLY after every required stage has
            // succeeded (including CallbacksCleared); otherwise the exact claim
            // and stage state are retained so the unresolved work is reattempted.
            if (tx.CallbacksCleared && tx.ContentCleared && tx.ButtonsResolved && tx.WindowsClosed && !tx.DataDetached)
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
        /// Return only buttons this attempt actually acquired, taken exclusively from
        /// the captured native-acquisition ledger. Membership in the shared active
        /// list or content hierarchy is deliberately NOT used: native Close leaves old
        /// foreign buttons in the active list, so list membership is not proof of
        /// acquisition by this attempt. An empty ledger after a genuine early native
        /// Open failure therefore leaves every foreign button untouched. Each captured
        /// item is tracked through Owned -> (Transferred | Uncertain) phases; membership
        /// is checked by reference identity before any mutation, an already returned
        /// item is never touched, and an uncertain item is only re-checked read-only. A
        /// lost capture record quarantines the transaction by never reporting the
        /// button stage complete.
        /// </summary>
        private ButtonProcessResult ProcessOwnedButtons(OwnedTransaction tx)
        {
            bool allDone = !tx.CaptureRecordFailed;

            foreach (ButtonState state in tx.Buttons.ToArray())
            {
                UIDialogWindowButton button = state.Button;

                if (state.Phase == ButtonPhase.Transferred)
                    continue;

                if (ReferenceEquals(button, null))
                {
                    // Actual CLR null: no managed identity remains to remove.
                    state.Phase = ButtonPhase.Transferred;
                    continue;
                }

                if (button == null)
                {
                    // Unity fake-null: the native object is destroyed. While our data
                    // claim is provably ours, drop the stale managed reference from
                    // the ORIGINAL active list without touching the destroyed Unity
                    // object, then end this item's ownership. Never mutate a foreign
                    // or taken-over list.
                    ButtonOwner destroyedOwner = ClassifyOwnership(tx);
                    if (destroyedOwner == ButtonOwner.Foreign)
                        return ButtonProcessResult.Relinquished;
                    if (destroyedOwner == ButtonOwner.Unreadable)
                    {
                        allDone = false;
                        continue;
                    }

                    if (!state.ListRemoved)
                        state.ListRemoved = RemoveFromActiveList(tx, button);

                    if (!state.ListRemoved)
                    {
                        allDone = false;
                        continue;
                    }

                    state.Phase = ButtonPhase.Transferred;
                    continue;
                }

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

        /// <summary>
        /// Record one button the native Open actually acquired for this transaction.
        /// Called only from the scoped provenance observer, before SetParent/Draw/list
        /// registration, so a ref acquired by this attempt is ours even when the open
        /// later fails or never attaches it. The original prefab is never owned.
        /// </summary>
        private static void RecordAcquiredButton(OwnedTransaction tx, UIDialogWindowButton button)
        {
            if (tx == null)
                return;

            // A CLR-null or the shared prefab is not a button this attempt owns.
            // Mark the provenance incomplete instead of inserting a ledger entry that
            // would fake count success; the prefab stays excluded from mutation.
            if (ReferenceEquals(button, null) || ReferenceEquals(button, tx.Prefab))
            {
                tx.CaptureRecordFailed = true;
                return;
            }

            EnsureButtonState(tx, button);
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
            // Remove ALL exact-reference occurrences (managed identity only).
            for (int i = list.Count - 1; i >= 0; i--)
            {
                if (ReferenceEquals(list[i], button))
                    list.RemoveAt(i);
            }

            // Verified absence is the required proof. An item that was already
            // absent (e.g. the native Open acquire-parent-Draw-before-Add orphan)
            // is idempotent success, not a removal that never happened.
            for (int i = 0; i < list.Count; i++)
            {
                if (ReferenceEquals(list[i], button))
                    return false;
            }

            return true;
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
            internal Action AffirmativeAction;

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

            // Provenance of this attempt's buttons. The ledger below is populated only
            // via the native-acquisition observer; there is no hierarchy discovery.
            internal int ExpectedAcquisitionCount;

            // True when this attempt's acquisition provenance is known lost or
            // ambiguous: the observer recorded no real owned button, the scope failed
            // to unwind, or a normal return did not match the expected count exactly.
            // It defaults false so a scope that never ran a native open still cleans
            // its own preclaim normally.
            internal bool CaptureRecordFailed;

            // A confirmed foreign takeover or genuine destruction is a distinct,
            // allowed ownership-end path that leaves only management cleanup.
            internal bool Relinquished;

            internal readonly List<ButtonState> Buttons = new List<ButtonState>();

            /// <summary>
            /// True only when the full expected acquisition set was captured exactly
            /// and no record was lost. A normal native return without it fails closed
            /// instead of writing de-duplication or declaring cleanup complete.
            /// </summary>
            internal bool CaptureExpectedMet =>
                !CaptureRecordFailed
                && ExpectedAcquisitionCount > 0
                && Buttons.Count == ExpectedAcquisitionCount;
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
