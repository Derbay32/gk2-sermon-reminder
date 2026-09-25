using System;
using System.Collections.Generic;
using System.Reflection;
using DG.Tweening;
using GK2.Framework;
using GK2.SermonReminder.Localization;
using HarmonyLib;
using LazyBearTechnology;
using TMPro;
using UnityEngine;

namespace GK2.SermonReminder.Notifications
{
    /// <summary>
    /// Owns the two independent fault-notice categories: the necessary state read
    /// and the corner HUD. Each category has exactly one display budget per genuine
    /// load; a category only spends its budget after a real native notification is
    /// verified as registered, active and actually rendering the localized text.
    ///
    /// The class never derives a fault from ordinary loading, exit, a missing
    /// native HUD or the documented missing-key default. The shared owner invokes
    /// <see cref="BeginLoad"/> and <see cref="EndLoad"/> only for genuine lifecycle
    /// transitions and passes the current fault flags to <see cref="Update"/>.
    ///
    /// A category queues a single pending notice while the surface is unsafe or the
    /// notifier is absent, cancels that queue without spending when the fault
    /// recovers, and may still display once on a later same-load refault if the
    /// budget was never spent.
    ///
    /// A native show that cannot be verified is kept as a single owned
    /// <see cref="OwnedNotice"/> transaction holding the acquired item plus the
    /// original notifier, backing list and pool identities. Its cleanup is retried
    /// idempotently and bounded before the item is returned to its original pool
    /// using pool membership identity; until the cleanup fully succeeds no new
    /// object may be allocated. A notice may never be shown while no load is active.
    /// </summary>
    internal sealed class SermonFaultNotices
    {
        /// <summary>Catalog semantic key for the state-read failure notice.</summary>
        internal const string StateReadStatusKey = "gksr.status.reminderUnavailable";

        /// <summary>Catalog semantic key for the corner-HUD failure notice.</summary>
        internal const string CornerHudStatusKey = "gksr.status.hudUnavailable";

        // Bounded retry for native display and cleanup failures, measured in
        // unscaled time so a paused game never spams native acquisition attempts.
        private const float RetryDelaySeconds = 2f;

        private readonly Gk2ModLogger log;

        // Native reflection, resolved lazily. A partial resolution is not cached as
        // success, so a native API that appears later can still be picked up on a
        // later bounded retry.
        private bool reflectionResolved;
        private MethodInfo showNotificationMethod;
        private MethodInfo updateNotificationsPositionMethod;
        private FieldInfo displayingItemsField;
        private FieldInfo labelField;

        private readonly CategoryState stateRead = new CategoryState(StateReadStatusKey);
        private readonly CategoryState cornerHud = new CategoryState(CornerHudStatusKey);

        // Active-load boundary: false until a genuine BeginLoad. A notice may never
        // be shown while inactive; retained cleanup may still be retried.
        private bool activeLoad;

        // At most one owned notice transaction exists at a time. While it is
        // unresolved it blocks every new allocation, across resets included.
        private OwnedNotice transaction;

        // Deduplicated entrypoint diagnostics.
        private string lastEntryLogKey;

        internal SermonFaultNotices(Gk2ModLogger log)
        {
            this.log = log;
        }

        /// <summary>Per-category budget and single-flight retry state.</summary>
        private sealed class CategoryState
        {
            internal readonly string StatusKey;
            internal bool BudgetSpent;
            internal float NextAttemptUnscaled;
            internal string LastLogKey;

            internal CategoryState(string statusKey)
            {
                StatusKey = statusKey;
            }
        }

        /// <summary>
        /// A notice acquired from the native pool, together with the frozen
        /// identities needed to unwind it safely. Ownership is retained until every
        /// cleanup step succeeds (or the item is destroyed).
        /// </summary>
        private sealed class OwnedNotice
        {
            internal readonly UISimpleTextNotification Item;
            internal readonly UINotificator Notificator;
            internal readonly List<UIBaseNotification> List;
            internal readonly Pool Pool;
            internal float NextCleanupUnscaled;

            internal OwnedNotice(
                UISimpleTextNotification item,
                UINotificator notificator,
                List<UIBaseNotification> list,
                Pool pool,
                float now)
            {
                Item = item;
                Notificator = notificator;
                List = list;
                Pool = pool;
                NextCleanupUnscaled = now;
            }
        }

        /// <summary>
        /// Genuine load transition. Opens the active-load boundary and resets both
        /// category budgets. An unresolved older transaction is deliberately kept:
        /// it must be cleaned before any new allocation, even across a reset.
        /// </summary>
        internal void BeginLoad()
        {
            try
            {
                activeLoad = true;
                ResetBudget(stateRead);
                ResetBudget(cornerHud);
                TryAdvanceTransactionCleanup();
            }
            catch (Exception ex)
            {
                LogEntryOnce("begin:" + ex.GetType().Name,
                    "GKSR_NOTICE_BEGIN_LOAD_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Genuine load end (menu return or save switch). Closes the active-load
        /// boundary so no later tick can show a notice, drops any undisplayed queue,
        /// and attempts the owned partial cleanup. A notice whose lifetime was
        /// already transferred to the native timer is never touched.
        /// </summary>
        internal void EndLoad()
        {
            try
            {
                activeLoad = false;
                CancelQueue(stateRead);
                CancelQueue(cornerHud);
                TryAdvanceTransactionCleanup();
            }
            catch (Exception ex)
            {
                LogEntryOnce("end:" + ex.GetType().Name,
                    "GKSR_NOTICE_END_LOAD_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Advance both categories for this tick. While no load is active the method
        /// only retries retained cleanup and never shows; otherwise both flags are
        /// processed every tick so a recovery cancels an undisplayed queue before a
        /// later refault may display.
        /// </summary>
        internal void Update(bool stateReadFailed, bool cornerHudFailed)
        {
            try
            {
                TryAdvanceTransactionCleanup();

                if (!activeLoad)
                    return;

                ProcessCategory(stateRead, stateReadFailed);
                ProcessCategory(cornerHud, cornerHudFailed);
            }
            catch (Exception ex)
            {
                // A notice problem must never escape into the caller's tick.
                LogEntryOnce("update:" + ex.GetType().Name,
                    "GKSR_NOTICE_UPDATE_FAILED: " + ex.GetType().Name);
            }
        }

        private static void ResetBudget(CategoryState category)
        {
            category.BudgetSpent = false;
            category.NextAttemptUnscaled = 0f;
            category.LastLogKey = null;
        }

        /// <summary>
        /// Cancel an undisplayed queue without spending. The diagnostic episode is
        /// intentionally preserved: an intermediate success never clears it.
        /// </summary>
        private static void CancelQueue(CategoryState category)
        {
            category.NextAttemptUnscaled = 0f;
        }

        private void ProcessCategory(CategoryState category, bool faulted)
        {
            if (!faulted)
            {
                // Recovery: cancel an undisplayed queue without spending. A later
                // same-load refault may still display if the budget was never spent.
                CancelQueue(category);
                return;
            }

            if (category.BudgetSpent)
            {
                // Already displayed once this load: no extra notice, even if the
                // fault continues or its underlying detail changes.
                return;
            }

            if (transaction != null)
            {
                // A previous failed notice still owns native resources: block every
                // new allocation until its cleanup resolves.
                LogOnce(category, "own-transaction-pending",
                    "GKSR_NOTICE_DEFERRED: " + category.StatusKey + " (previous notice transaction unresolved)");
                return;
            }

            if (!TryGetUnscaledTime(out float now))
            {
                LogOnce(category, "clock-unreadable",
                    "GKSR_NOTICE_DEFERRED: " + category.StatusKey + " (unscaled clock unreadable)");
                return;
            }

            if (now < category.NextAttemptUnscaled)
                return;

            if (!TryPrepareAttempt(category, now,
                out UINotificator notificator, out Pool pool,
                out List<UIBaseNotification> list, out string text))
            {
                return;
            }

            AttemptDisplay(category, notificator, pool, list, text, now);
        }

        private bool TryPrepareAttempt(
            CategoryState category,
            float now,
            out UINotificator notificator,
            out Pool pool,
            out List<UIBaseNotification> list,
            out string text)
        {
            notificator = null;
            pool = null;
            list = null;
            text = null;

            if (!IsNoticeSurfaceSafe())
            {
                // Cheap per-tick deferral: covers menu, cinematic, dialogue, modal
                // and non-modal busy windows, pause, loading overlay and a
                // non-controllable player. Log is deduplicated.
                LogOnce(category, "unsafe-ui",
                    "GKSR_NOTICE_DEFERRED: " + category.StatusKey + " (native UI not safe for a notice)");
                return false;
            }

            if (!EnsureReflection())
            {
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "reflection-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notifier API unavailable)");
                return false;
            }

            notificator = TryGetNotificator();
            if (notificator == null)
            {
                // The native notifier object is genuinely absent: log only, defer,
                // and never fabricate a success or spend the budget.
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "notifier-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notifier object absent)");
                return false;
            }

            if (!TryGetDisplayingList(notificator, out list))
            {
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "list-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notice list unavailable)");
                return false;
            }

            text = SermonReminderLocalization.Get(category.StatusKey);
            if (string.IsNullOrEmpty(text))
            {
                // A missing localized sentence means we cannot show anything. We
                // never substitute fallback copy or mutate the global dictionary.
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "missing-text",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (localized text unavailable)");
                return false;
            }

            pool = TryGetPool();
            if (pool == null)
            {
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "pool-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notice pool unavailable)");
                return false;
            }

            return true;
        }

        private void AttemptDisplay(
            CategoryState category,
            UINotificator notificator,
            Pool pool,
            List<UIBaseNotification> list,
            string text,
            float now)
        {
            UISimpleTextNotification item;
            try
            {
                // Inactive acquisition: the object is not activated until the native
                // ShowNotification path activates, positions and draws it.
                item = pool.GetOrCreateInactiveObject<UISimpleTextNotification>();
            }
            catch (Exception ex)
            {
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "acquire-failed:" + ex.GetType().Name,
                    "GKSR_NOTICE_SHOW_FAILED: " + category.StatusKey + " (acquire: " + ex.GetType().Name + ")");
                return;
            }

            if (item == null)
            {
                category.NextAttemptUnscaled = now + RetryDelaySeconds;
                LogOnce(category, "acquire-empty",
                    "GKSR_NOTICE_SHOW_FAILED: " + category.StatusKey + " (native notice pool returned no object)");
                return;
            }

            // Persist the complete owned transaction immediately, before assigning
            // text, showing or logging, so no later throw can leak the acquired item.
            transaction = new OwnedNotice(item, notificator, list, pool, now);

            bool displayed;
            string failure;
            try
            {
                item.LocalizationKey = category.StatusKey;
                item.Text = text;
                InvokeShow(notificator, item);
                if (IsRegisteredActiveAndRendered(list, item, text))
                {
                    displayed = true;
                    failure = null;
                }
                else
                {
                    displayed = false;
                    failure = "not-verified";
                }
            }
            catch (Exception ex)
            {
                displayed = false;
                failure = ex.GetType().Name;
            }

            if (displayed)
            {
                // Verified real display: consume the budget, transfer the object's
                // lifetime to the native 4s notification/tween logic, and clear our
                // own transaction BEFORE the optional log. Dropping the reference
                // means a later pooled reuse can never be mistaken for this notice.
                category.BudgetSpent = true;
                category.NextAttemptUnscaled = 0f;
                transaction = null;
                SafeInfo("GKSR_NOTICE_DISPLAYED: category=" + category.StatusKey);
                return;
            }

            category.NextAttemptUnscaled = now + RetryDelaySeconds;
            LogOnce(category, "show-failed:" + failure,
                "GKSR_NOTICE_SHOW_FAILED: " + category.StatusKey + " (" + failure + ")");

            // Keep the failed attempt as our own transaction and try to unwind it.
            TryAdvanceTransactionCleanup();
        }

        /// <summary>
        /// Bounded, idempotent retry of the single owned transaction. It never
        /// allocates and never drops the transaction before cleanup succeeds; a
        /// genuinely destroyed item ends ownership.
        /// </summary>
        private void TryAdvanceTransactionCleanup()
        {
            OwnedNotice owned = transaction;
            if (owned == null)
                return;

            if (owned.Item == null)
            {
                // Unity destroyed the object: nothing can be returned, but its stale
                // entry must still be removed from the original backing list.
                TryRemoveFromListOnly(owned);
                transaction = null;
                return;
            }

            if (!TryGetUnscaledTime(out float now))
                return;

            if (now < owned.NextCleanupUnscaled)
                return;

            if (TryCompleteCleanup(owned))
            {
                transaction = null;
                return;
            }

            owned.NextCleanupUnscaled = now + RetryDelaySeconds;
        }

        /// <summary>
        /// Perform every required cleanup step idempotently against the frozen
        /// original identities. Returns true only when the item has been fully
        /// detached from the native list and verifiably returned to its original
        /// pool. Any critical failure keeps the transaction for a bounded retry.
        /// </summary>
        private bool TryCompleteCleanup(OwnedNotice owned)
        {
            UISimpleTextNotification item = owned.Item;
            if (item == null)
                return true;

            // 1. Remove ONLY our exact instance from the original backing list. A
            //    destroyed notifier has no live list or tween to detach from, so only
            //    the exact item is recovered instead of blocking forever.
            bool notificatorAlive = owned.Notificator != null;
            if (notificatorAlive)
            {
                if (!TryRemoveFromList(owned))
                    return false;

                // 2. Reposition the remaining native notices via the exact native
                //    method. Required: it is resolved before acquisition, so a missing
                //    method here is a critical failure that keeps the transaction.
                MethodInfo reposition = updateNotificationsPositionMethod;
                if (reposition == null)
                    return false;
                try
                {
                    reposition.Invoke(owned.Notificator, null);
                }
                catch (Exception)
                {
                    return false;
                }
            }

            // 3. Stop our timer and kill ONLY our own transform tween; other
            //    notices' content and timers are never altered.
            try { item.IsTimerActive = false; } catch (Exception) { return false; }
            try { item.transform.DOKill(); } catch (Exception) { return false; }

            // 4. Clear the actual rendered label plus the assigned text and key.
            try
            {
                if (labelField?.GetValue(item) as TextMeshProUGUI is TextMeshProUGUI label)
                    label.text = string.Empty;
            }
            catch (Exception)
            {
                return false;
            }
            try { item.Text = null; } catch (Exception) { return false; }
            try { item.LocalizationKey = null; } catch (Exception) { return false; }

            // 5. Hide before returning to the pool.
            try { item.gameObject.SetActive(false); } catch (Exception) { return false; }

            // 6. Return to the ORIGINAL pool using membership identity, never a
            //    freshly resolved pool that a replacement could have changed.
            return TryReturnToOriginalPool(owned);
        }

        private bool TryRemoveFromList(OwnedNotice owned)
        {
            try
            {
                RemoveFromList(owned);
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void TryRemoveFromListOnly(OwnedNotice owned)
        {
            try { RemoveFromList(owned); }
            catch (Exception) { }
        }

        private static void RemoveFromList(OwnedNotice owned)
        {
            List<UIBaseNotification> list = owned.List;
            if (list == null)
                return;

            UISimpleTextNotification item = owned.Item;
            for (int i = list.Count - 1; i >= 0; i--)
            {
                if (ReferenceEquals(list[i], item))
                    list.RemoveAt(i);
            }
        }

        /// <summary>
        /// Return the item to the exact pool it was acquired from, using reference
        /// membership in <c>Pool.Objects</c> before and after the release. An item
        /// already present is never released again; an unreadable membership is not
        /// proof of return, so ownership is preserved for a later bounded retry.
        /// </summary>
        private static bool TryReturnToOriginalPool(OwnedNotice owned)
        {
            UISimpleTextNotification item = owned.Item;
            if (item == null)
                return true;

            Pool pool = owned.Pool;
            if (pool == null)
                return false;

            bool? present = IsInPool(pool, item);
            if (present == null)
                return false;
            if (present.Value)
                return true;

            try { pool.ReleaseObject(item); }
            catch (Exception)
            {
                // The push happens before reparent/deactivate, so the membership
                // re-check below decides whether the object is actually in the pool.
            }

            present = IsInPool(pool, item);
            if (present == null)
                return false;
            return present.Value;
        }

        /// <summary>
        /// Reference membership of the exact item in the pool's idle stack.
        /// Returns null when membership cannot be read (never treated as returned).
        /// </summary>
        private static bool? IsInPool(Pool pool, UISimpleTextNotification item)
        {
            try
            {
                Stack<MonoBehaviour> objects = pool.Objects;
                if (objects == null)
                    return null;

                foreach (MonoBehaviour candidate in objects)
                {
                    if (ReferenceEquals(candidate, item))
                        return true;
                }
                return false;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private bool EnsureReflection()
        {
            if (reflectionResolved)
                return true;

            try { showNotificationMethod = AccessTools.Method(typeof(UINotificator), "ShowNotification"); }
            catch (Exception) { showNotificationMethod = null; }

            try { updateNotificationsPositionMethod = AccessTools.Method(typeof(UINotificator), "UpdateNotificationsPosition"); }
            catch (Exception) { updateNotificationsPositionMethod = null; }

            try { displayingItemsField = AccessTools.Field(typeof(UINotificator), "displayingItems"); }
            catch (Exception) { displayingItemsField = null; }

            try { labelField = AccessTools.Field(typeof(UISimpleTextNotification), "label"); }
            catch (Exception) { labelField = null; }

            reflectionResolved = showNotificationMethod != null
                && updateNotificationsPositionMethod != null
                && displayingItemsField != null
                && labelField != null;
            return reflectionResolved;
        }

        private void InvokeShow(UINotificator notificator, UISimpleTextNotification item)
        {
            showNotificationMethod.Invoke(notificator, new object[] { item });
        }

        private bool TryGetDisplayingList(UINotificator notificator, out List<UIBaseNotification> list)
        {
            list = null;
            try
            {
                list = displayingItemsField?.GetValue(notificator) as List<UIBaseNotification>;
            }
            catch (Exception)
            {
                list = null;
            }
            return list != null;
        }

        /// <summary>
        /// A display only counts when the object is really registered in the frozen
        /// backing list, the native object is active, and the actual label component
        /// carries the resolved text (not merely the assigned Text property).
        /// </summary>
        private bool IsRegisteredActiveAndRendered(
            List<UIBaseNotification> list, UISimpleTextNotification item, string text)
        {
            try
            {
                if (item == null || !item.gameObject.activeInHierarchy)
                    return false;

                if (list == null)
                    return false;

                bool registered = false;
                for (int i = 0; i < list.Count; i++)
                {
                    if (ReferenceEquals(list[i], item))
                    {
                        registered = true;
                        break;
                    }
                }
                if (!registered)
                    return false;

                if (!(labelField?.GetValue(item) is TextMeshProUGUI label) || label == null)
                    return false;

                return string.Equals(label.text, text, StringComparison.Ordinal);
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// The native UI safety gate. In addition to pause, modal windows, the
        /// loading overlay and a non-controllable player, an active non-modal busy
        /// window (<c>ActiveWindow != null</c>) is not a safe notice surface.
        /// </summary>
        private static bool IsNoticeSurfaceSafe()
        {
            try
            {
                MainGame game = MainGame.Instance;
                if (game == null || game.gameState != MainGame.GameState.InGame)
                    return false;

                if (MainGame.IsGamePaused)
                    return false;

                if (LazyWindowsStackController.HasAnyModalWindowOpened)
                    return false;

                if (LazyWindowsStackController.ActiveWindow != null)
                    return false;

                if (IsLoadingOverlayShown())
                    return false;

                return IsPlayerControllable();
            }
            catch (Exception)
            {
                // Unreadable safety facts are treated as unsafe (defer), never safe.
                return false;
            }
        }

        private static bool IsLoadingOverlayShown()
        {
            try
            {
                UILoadingOverlay overlay = LazyUI.Get<UILoadingOverlay>();
                return overlay != null && overlay.IsShown;
            }
            catch (Exception)
            {
                // The overlay cannot be resolved: not provably safe.
                return true;
            }
        }

        private static bool IsPlayerControllable()
        {
            try
            {
                PlayerController player = MainGame.PlayerController;
                return player != null && player.IsControlsEnabled;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static UINotificator TryGetNotificator()
        {
            try
            {
                // Resolve the live registered native element; never create one, so an
                // absent notifier stays an honest absence rather than a fabrication.
                return LazyUI.Get<UINotificator>();
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static Pool TryGetPool()
        {
            try
            {
                return LazyPooler.GetPoolByType<UISimpleTextNotification>();
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>
        /// Read the unscaled clock. A non-finite value is rejected so no backoff can
        /// be scheduled from a garbage time; the caller then defers.
        /// </summary>
        private static bool TryGetUnscaledTime(out float now)
        {
            now = 0f;
            try
            {
                float value = Time.unscaledTime;
                if (float.IsNaN(value) || float.IsInfinity(value))
                    return false;

                now = value;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void LogOnce(CategoryState category, string key, string message)
        {
            if (string.Equals(category.LastLogKey, key, StringComparison.Ordinal))
                return;

            category.LastLogKey = key;
            SafeWarning(message);
        }

        private void LogEntryOnce(string key, string message)
        {
            if (string.Equals(lastEntryLogKey, key, StringComparison.Ordinal))
                return;

            lastEntryLogKey = key;
            SafeWarning(message);
        }

        /// <summary>Logging can never throw out of an entrypoint or abort cleanup.</summary>
        private void SafeWarning(string message)
        {
            try { log?.Warning(message); }
            catch (Exception) { }
        }

        /// <summary>Logging can never throw out of an entrypoint or abort cleanup.</summary>
        private void SafeInfo(string message)
        {
            try { log?.Info(message); }
            catch (Exception) { }
        }
    }
}
