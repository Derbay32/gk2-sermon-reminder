using System;
using UnityEngine;
using UnityEngine.AddressableAssets;
using UnityEngine.AddressableAssets.ResourceLocators;
using UnityEngine.ResourceManagement.AsyncOperations;

namespace GK2.SermonReminder.Hud
{
    /// <summary>Owning state of the one game-sprite request.</summary>
    internal enum NativeCheckSpriteStatus
    {
        /// <summary>Not eligible (or nothing held): no request in flight.</summary>
        Idle,

        /// <summary>Request in flight; the Sprite is not usable yet.</summary>
        Pending,

        /// <summary>Resolved with a usable Sprite.</summary>
        Ready,

        /// <summary>Request/load failed; a bounded retry is scheduled.</summary>
        Failed
    }

    /// <summary>Result of advancing the owned sprite request once.</summary>
    internal readonly struct NativeCheckSpritePoll
    {
        internal NativeCheckSpriteStatus Status { get; }
        internal Sprite Sprite { get; }

        internal NativeCheckSpritePoll(NativeCheckSpriteStatus status, Sprite sprite)
        {
            Status = status;
            Sprite = sprite;
        }
    }

    /// <summary>
    /// Owns exactly one <see cref="AsyncOperationHandle{Sprite}"/> for the native
    /// "done" check sprite, addressed by GUID. It never initializes Addressables
    /// itself, never waits synchronously, never registers callbacks and never
    /// touches the shared Sprite/material it receives: the request is advanced by
    /// repeated polling so a released request can never call back and resurrect
    /// stale UI.
    ///
    /// Every native asset access is contained here: an unexpected Addressables
    /// failure releases the owned handle and degrades to <see cref="Failed"/>
    /// instead of escaping into the caller's tick, so an unrelated failure can
    /// never tear down a healthy Ready/countdown presentation. An unreadable
    /// unscaled clock is treated the same way as a controlled resource failure and
    /// recovers once the clock can be read again.
    ///
    /// The handle is created lazily once the caller reports eligibility (sermon
    /// day, gates open, Ready or Done) and retained while Ready/Done, so a cold
    /// load does not start at the moment the sermon is consumed. Failures release
    /// the owned handle immediately, record one bounded failure episode, and retry
    /// no sooner than <see cref="RetryDelaySeconds"/> of unscaled time while still
    /// eligible.
    /// </summary>
    internal sealed class NativeCheckSprite
    {
        /// <summary>Catalog-verified Sprite GUID for btn_i-check.</summary>
        internal const string CheckSpriteGuid = "4d1c67d72f3e6c8419b24e8c275d101e";

        // Bounded retry backoff measured in unscaled time.
        private const float RetryDelaySeconds = 2f;

        private AsyncOperationHandle<Sprite> handle;
        private bool ownsHandle;

        private bool failed;
        private float nextRetryUnscaledTime;
        private string lastFailure;

        /// <summary>The last failure reason, or null while healthy.</summary>
        internal string LastFailure => lastFailure;

        /// <summary>
        /// Advance the request and return the current ownership state.
        ///
        /// When <paramref name="eligible"/> is false the owned handle and the whole
        /// failure episode are cleared, so a later eligibility never inherits the
        /// previous load's failure or cooldown. When it is true the single handle is
        /// kept across ticks; a request is only issued when none is owned and no
        /// failure backoff is pending.
        /// </summary>
        internal NativeCheckSpritePoll Poll(bool eligible)
        {
            if (!eligible)
            {
                Release();
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Idle, null);
            }

            if (ownsHandle)
                return PollOwned();

            if (failed)
            {
                if (!TryUnscaledTime(out float now))
                {
                    // The clock is unreadable, so the backoff cannot be evaluated.
                    // Report the failure without issuing another request; the next
                    // readable clock recovers the retry.
                    return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
                }

                if (now < nextRetryUnscaledTime)
                    return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            // Retry window open (or first request): clear the stale failure so a
            // successful request reports a clean recovery.
            ClearEpisode();
            return Request();
        }

        /// <summary>
        /// Release the owned handle and clear the failure episode. Used on exit,
        /// load replacement, eligibility loss and HUD rebinding, so ownership and
        /// diagnostics never carry across into a new binding.
        /// </summary>
        internal void Release()
        {
            ReleaseHandleOnly();
            ClearEpisode();
        }

        private NativeCheckSpritePoll Request()
        {
            if (!HasNativeResourceLocators())
            {
                MarkFailed("addressables not initialized");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            try
            {
                handle = Addressables.LoadAssetAsync<Sprite>(CheckSpriteGuid);
                ownsHandle = true;
            }
            catch (Exception ex)
            {
                handle = default;
                ownsHandle = false;
                MarkFailed(ex.GetType().Name);
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            // The request may already be complete; resolve it on this same tick.
            return PollOwned();
        }

        private NativeCheckSpritePoll PollOwned()
        {
            if (!TryHandleValid())
            {
                // Ownership was invalidated outside this instance: drop the
                // bookkeeping and schedule a bounded retry.
                ReleaseHandleOnly();
                MarkFailed("addressable handle invalidated");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (!TryHandleStatus(out AsyncOperationStatus status))
            {
                ReleaseHandleOnly();
                MarkFailed("addressable status unreadable");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (status == AsyncOperationStatus.Failed)
            {
                ReleaseHandleOnly();
                MarkFailed("addressable load failed");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (!TryHandleDone(out bool done))
            {
                ReleaseHandleOnly();
                MarkFailed("addressable completion unreadable");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (!done)
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Pending, null);

            Sprite sprite = null;
            string resultFailure = null;
            try
            {
                sprite = handle.Result;
            }
            catch (Exception ex)
            {
                sprite = null;
                resultFailure = ex.GetType().Name;
            }

            if (sprite == null)
            {
                ReleaseHandleOnly();
                MarkFailed(resultFailure ?? "addressable returned no sprite");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            ClearEpisode();
            return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Ready, sprite);
        }

        /// <summary>Release only the owned handle, keeping any failure episode.</summary>
        private void ReleaseHandleOnly()
        {
            // Clear ownership first so a throwing Release can never leave the old
            // handle reachable through this instance.
            AsyncOperationHandle<Sprite> owned = handle;
            handle = default;
            bool hadOwned = ownsHandle;
            ownsHandle = false;

            if (!hadOwned) return;

            try
            {
                Addressables.Release(owned);
            }
            catch (Exception)
            {
                // A native release failure must not fault the mod; the handle is
                // already dropped above so it can never be released twice.
            }
        }

        private void ClearEpisode()
        {
            failed = false;
            lastFailure = null;
            nextRetryUnscaledTime = 0f;
        }

        private void MarkFailed(string reason)
        {
            failed = true;
            lastFailure = reason;

            // Without a readable clock no backoff can be scheduled; reporting the
            // failure (and issuing no new request while the clock stays unreadable)
            // is the controlled degradation.
            nextRetryUnscaledTime = TryUnscaledTime(out float now) ? now + RetryDelaySeconds : 0f;
        }

        private bool TryHandleValid()
        {
            try
            {
                return handle.IsValid();
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool TryHandleStatus(out AsyncOperationStatus status)
        {
            status = default;
            try
            {
                status = handle.Status;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool TryHandleDone(out bool done)
        {
            done = false;
            try
            {
                done = handle.IsDone;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// True when the game has already published at least one resource locator.
        /// Addressables is never initialized here, and no locator is added.
        /// </summary>
        private static bool HasNativeResourceLocators()
        {
            try
            {
                foreach (IResourceLocator locator in Addressables.ResourceLocators)
                {
                    if (locator != null) return true;
                }
            }
            catch (Exception)
            {
                return false;
            }

            return false;
        }

        /// <summary>
        /// Read the unscaled clock. Returns false when the native clock cannot be
        /// read, so the caller never fabricates a time value.
        /// </summary>
        private static bool TryUnscaledTime(out float unscaledTime)
        {
            unscaledTime = 0f;
            try
            {
                unscaledTime = Time.unscaledTime;
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }
}
