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
    /// budget was never spent. A failed native show never spends and is recovered as
    /// our own transaction: only our instance is removed from the native list, its
    /// timer and tweens are stopped, its text and label are cleared, and it is
    /// returned to its original pool without ever being pushed twice.
    /// </summary>
    internal sealed class SermonFaultNotices
    {
        /// <summary>Catalog semantic key for the state-read failure notice.</summary>
        internal const string StateReadStatusKey = "gksr.status.reminderUnavailable";

        /// <summary>Catalog semantic key for the corner-HUD failure notice.</summary>
        internal const string CornerHudStatusKey = "gksr.status.hudUnavailable";

        // Bounded retry for native display failures, measured in unscaled time so
        // a paused game never spams acquisition attempts.
        private const float RetryDelaySeconds = 2f;

        private readonly Gk2ModLogger log;

        // Native reflection, resolved lazily once. A missing member is an ordinary
        // unavailable surface: defer, never spend and never fabricate a notice.
        private bool reflectionResolved;
        private MethodInfo showNotificationMethod;
        private MethodInfo updateNotificationsPositionMethod;
        private FieldInfo displayingItemsField;
        private FieldInfo labelField;

        private readonly CategoryState stateRead = new CategoryState(StateReadStatusKey);
        private readonly CategoryState cornerHud = new CategoryState(CornerHudStatusKey);

        // A failed transaction we could not verifiably return to its original pool.
        // While set, no new object may be allocated, even across a load reset.
        private UISimpleTextNotification orphanItem;
        private Pool orphanPool;

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
        /// Genuine load transition. Clears both budgets and any undisplayed queue so a
        /// fresh load starts with two unspent budgets and no carried notice. The
        /// partial-transaction cleanup is attempted; an unreturned orphan keeps
        /// blocking allocation until it can be safely returned.
        /// </summary>
        internal void BeginLoad()
        {
            try
            {
                ResetForNewLoad(stateRead);
                ResetForNewLoad(cornerHud);
                TryFinalizeOrphan();
            }
            catch (Exception ex)
            {
                log?.Warning("GKSR_NOTICE_BEGIN_LOAD_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Genuine load end (menu return or save switch). Drops any undisplayed
        /// queue so no stale notice can survive, and never touches a notification
        /// whose lifetime was already transferred to the native timer.
        /// </summary>
        internal void EndLoad()
        {
            try
            {
                CancelQueue(stateRead);
                CancelQueue(cornerHud);
            }
            catch (Exception ex)
            {
                log?.Warning("GKSR_NOTICE_END_LOAD_FAILED: " + ex.GetType().Name);
            }
        }

        /// <summary>
        /// Advance both categories for this tick. The flags are the current fault
        /// facts; both are processed every tick so a recovery cancels an undisplayed
        /// queue before a later refault may display.
        /// </summary>
        internal void Update(bool stateReadFailed, bool cornerHudFailed)
        {
            try
            {
                TryFinalizeOrphan();
                ProcessCategory(stateRead, stateReadFailed);
                ProcessCategory(cornerHud, cornerHudFailed);
            }
            catch (Exception ex)
            {
                // A notice problem must never escape into the caller's tick.
                log?.Warning("GKSR_NOTICE_UPDATE_FAILED: " + ex.GetType().Name);
            }
        }

        private void ResetForNewLoad(CategoryState category)
        {
            category.BudgetSpent = false;
            category.NextAttemptUnscaled = 0f;
            category.LastLogKey = null;
        }

        private static void CancelQueue(CategoryState category)
        {
            category.NextAttemptUnscaled = 0f;
            category.LastLogKey = null;
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

            // Fault present with an unspent budget: at most one notice per category
            // is possible because each category owns a single (not a list) slot.
            if (orphanItem != null)
            {
                LogOnce(category, "orphan-pending",
                    "GKSR_NOTICE_DEFERRED: " + category.StatusKey + " (previous failed notice not yet returned)");
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

            if (!TryPrepareAttempt(category, now, out UINotificator notificator, out Pool pool, out string text))
                return;

            AttemptDisplay(category, notificator, pool, text, now);
        }

        private bool TryPrepareAttempt(
            CategoryState category, float now,
            out UINotificator notificator, out Pool pool, out string text)
        {
            notificator = null;
            pool = null;
            text = null;

            if (!IsNoticeSurfaceSafe())
            {
                // Covers menu, cinematic, dialogue, modal and non-modal busy
                // windows, pause, loading overlay and a non-controllable player.
                LogOnce(category, "unsafe-ui",
                    "GKSR_NOTICE_DEFERRED: " + category.StatusKey + " (native UI not safe for a notice)");
                return false;
            }

            notificator = TryGetNotificator();
            if (notificator == null)
            {
                // The native notifier object is genuinely absent: log only, defer,
                // and never fabricate a success or spend the budget.
                LogOnce(category, "notifier-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notifier object absent)");
                return false;
            }

            text = SermonReminderLocalization.Get(category.StatusKey);
            if (string.IsNullOrEmpty(text))
            {
                // A missing localized sentence means we cannot show anything. We
                // never substitute fallback copy or mutate the global dictionary.
                LogOnce(category, "missing-text",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (localized text unavailable)");
                return false;
            }

            if (!EnsureReflection())
            {
                LogOnce(category, "reflection-absent",
                    "GKSR_NOTICE_UNAVAILABLE: " + category.StatusKey + " (native notifier API unavailable)");
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
            CategoryState category, UINotificator notificator, Pool pool, string text, float now)
        {
            UISimpleTextNotification item = null;
            try
            {
                // Inactive acquisition: the object is not activated until the native
                // ShowNotification path activates, positions and draws it.
                item = pool.GetOrCreateInactiveObject<UISimpleTextNotification>();
            }
            catch (Exception ex)
            {
                item = null;
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

            bool displayed;
            string failure;
            try
            {
                item.LocalizationKey = category.StatusKey;
                item.Text = text;
                InvokeShow(notificator, item);
                if (IsRegisteredActiveAndRendered(notificator, item, text))
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
                // Verified real display: consume the category budget and transfer the
                // object's lifetime to the native 4s notification/tween logic. Dropping
                // our reference means a later pooled reuse can never be mistaken for
                // this notice.
                category.BudgetSpent = true;
                category.NextAttemptUnscaled = 0f;
                category.LastLogKey = null;
                log?.Info("GKSR_NOTICE_DISPLAYED: category=" + category.StatusKey);
                return;
            }

            LogOnce(category, "show-failed:" + failure,
                "GKSR_NOTICE_SHOW_FAILED: " + category.StatusKey + " (" + failure + ")");

            // Keep the failed attempt as our own transaction and recover it fully.
            RecoverFailedTransaction(notificator, pool, item);
            category.NextAttemptUnscaled = now + RetryDelaySeconds;
        }

        private void RecoverFailedTransaction(UINotificator notificator, Pool pool, UISimpleTextNotification item)
        {
            if (item == null)
                return;

            // Remove ONLY our instance from the actual native backing list; other
            // messages' content and timers are never touched.
            try
            {
                var list = displayingItemsField?.GetValue(notificator) as List<UIBaseNotification>;
                list?.Remove(item);
            }
            catch (Exception)
            {
            }

            try { item.IsTimerActive = false; } catch (Exception) { }
            try { item.transform.DOKill(); } catch (Exception) { }

            try
            {
                var label = labelField?.GetValue(item) as TextMeshProUGUI;
                if (label != null) label.text = string.Empty;
            }
            catch (Exception)
            {
            }

            try { item.Text = null; } catch (Exception) { }
            try { item.LocalizationKey = null; } catch (Exception) { }
            try { item.gameObject.SetActive(false); } catch (Exception) { }

            ReturnToPool(item, pool);

            try { updateNotificationsPositionMethod?.Invoke(notificator, null); } catch (Exception) { }
        }

        /// <summary>
        /// Return an item to its original pool, detecting a partially completed push
        /// from the pool size so the same object is never pushed twice. A push that
        /// cannot be verified is retained as an orphan that blocks allocation.
        /// </summary>
        private void ReturnToPool(UISimpleTextNotification item, Pool pool)
        {
            if (item == null)
                return;

            if (pool == null)
            {
                orphanItem = item;
                orphanPool = null;
                return;
            }

            int before = SafePoolCount(pool);
            try
            {
                pool.ReleaseObject(item);
            }
            catch (Exception)
            {
                // The push happens before reparent/deactivate, so the count delta
                // below decides whether the object is actually in the pool.
            }

            int after = SafePoolCount(pool);
            if (after == before + 1)
            {
                if (ReferenceEquals(orphanItem, item))
                {
                    orphanItem = null;
                    orphanPool = null;
                }
                return;
            }

            orphanItem = item;
            orphanPool = pool;
        }

        private void TryFinalizeOrphan()
        {
            if (orphanItem == null)
            {
                // Also clears a Unity-destroyed reference so no stale own ref remains.
                orphanItem = null;
                orphanPool = null;
                return;
            }

            Pool pool = orphanPool ?? TryGetPool();
            if (pool == null)
                return;

            UISimpleTextNotification item = orphanItem;
            orphanItem = null;
            orphanPool = null;
            ReturnToPool(item, pool);
        }

        private static int SafePoolCount(Pool pool)
        {
            try
            {
                Stack<MonoBehaviour> objects = pool.Objects;
                return objects == null ? -1 : objects.Count;
            }
            catch (Exception)
            {
                return -1;
            }
        }

        private bool EnsureReflection()
        {
            if (!reflectionResolved)
            {
                reflectionResolved = true;
                try { showNotificationMethod = AccessTools.Method(typeof(UINotificator), "ShowNotification"); }
                catch (Exception) { showNotificationMethod = null; }

                try { updateNotificationsPositionMethod = AccessTools.Method(typeof(UINotificator), "UpdateNotificationsPosition"); }
                catch (Exception) { updateNotificationsPositionMethod = null; }

                try { displayingItemsField = AccessTools.Field(typeof(UINotificator), "displayingItems"); }
                catch (Exception) { displayingItemsField = null; }

                try { labelField = AccessTools.Field(typeof(UISimpleTextNotification), "label"); }
                catch (Exception) { labelField = null; }
            }

            return showNotificationMethod != null
                && displayingItemsField != null
                && labelField != null;
        }

        private void InvokeShow(UINotificator notificator, UISimpleTextNotification item)
        {
            showNotificationMethod.Invoke(notificator, new object[] { item });
        }

        /// <summary>
        /// A display only counts when the object is really registered in the native
        /// backing list, the native object is active, and the actual label component
        /// carries the resolved text (not merely the assigned Text property).
        /// </summary>
        private bool IsRegisteredActiveAndRendered(
            UINotificator notificator, UISimpleTextNotification item, string text)
        {
            try
            {
                if (item == null || !item.gameObject.activeInHierarchy)
                    return false;

                var list = displayingItemsField?.GetValue(notificator) as List<UIBaseNotification>;
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

                var label = labelField?.GetValue(item) as TextMeshProUGUI;
                if (label == null)
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
                UINotificator notificator = LazyUI.Get<UINotificator>();
                return notificator;
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

        private static bool TryGetUnscaledTime(out float now)
        {
            now = 0f;
            try
            {
                now = Time.unscaledTime;
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
            log?.Warning(message);
        }
    }
}
