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
    /// The handle is created lazily once the caller reports eligibility (sermon
    /// day, gates open, Ready or Done) and retained while Ready/Done, so a cold
    /// load does not start at the moment the sermon is consumed. Failures release
    /// the owned handle immediately, record the failure, and retry no sooner than
    /// <see cref="RetryDelaySeconds"/> of unscaled time while still eligible.
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
        /// When <paramref name="eligible"/> is false the owned handle is released
        /// and nothing is requested. When it is true the single handle is kept
        /// across ticks; a request is only issued when none is owned and no
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

            if (failed && UnscaledTime() < nextRetryUnscaledTime)
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);

            // Retry window open (or first request): clear the stale failure so a
            // successful request reports a clean recovery.
            failed = false;
            lastFailure = null;
            return Request();
        }

        /// <summary>Release the owned handle without touching shared assets.</summary>
        internal void Release()
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
            if (!handle.IsValid())
            {
                // Ownership was invalidated outside this instance: drop the
                // bookkeeping and schedule a bounded retry.
                handle = default;
                ownsHandle = false;
                MarkFailed("addressable handle invalidated");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (handle.Status == AsyncOperationStatus.Failed)
            {
                Release();
                MarkFailed("addressable load failed");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            if (!handle.IsDone)
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Pending, null);

            Sprite sprite = null;
            try
            {
                sprite = handle.Result;
            }
            catch (Exception ex)
            {
                sprite = null;
                lastFailure = ex.GetType().Name;
            }

            if (sprite == null)
            {
                Release();
                MarkFailed(lastFailure ?? "addressable returned no sprite");
                return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Failed, null);
            }

            failed = false;
            lastFailure = null;
            return new NativeCheckSpritePoll(NativeCheckSpriteStatus.Ready, sprite);
        }

        private void MarkFailed(string reason)
        {
            failed = true;
            lastFailure = reason;
            nextRetryUnscaledTime = UnscaledTime() + RetryDelaySeconds;
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

        private static float UnscaledTime()
        {
            try
            {
                return Time.unscaledTime;
            }
            catch (Exception)
            {
                return 0f;
            }
        }
    }
}
