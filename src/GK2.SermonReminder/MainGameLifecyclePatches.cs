using System;
using HarmonyLib;

namespace GK2.SermonReminder
{
    /// <summary>
    /// Early Harmony prefixes on the native load / menu transitions, plus the
    /// contained deletion hook. The load prefixes run before the original methods
    /// finish, so the prior load is ended (and its popup work released) before a new
    /// save data becomes authoritative and the immutable entry identity is captured.
    /// Every native slot read is contained here, never evaluated unguarded as a
    /// method argument. Only our own patches are installed and removed.
    /// </summary>
    internal static class MainGameLifecyclePatches
    {
        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.ContinueGame))]
        private static void ContinueGamePrefix(SaveSlotData saveSlotData)
            => SermonReminderMod.Active?.HandleLoadEntry(saveSlotData);

        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.CreateGameSaveAndStart))]
        private static void CreateGameSaveAndStartPrefix(MainGame __instance)
            => SermonReminderMod.Active?.HandleNewGameLoadEntry(__instance);

        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.GoToMenu))]
        private static void GoToMenuPrefix() => SermonReminderMod.Active?.HandleReturnedToMenu();

        /// <summary>
        /// Freeze the deletion identity and the exact owning mod before the native
        /// removal runs, so a callback cannot route the result to an unrelated newer
        /// owner. The state is valid only when both fields are captured and the slot
        /// name is nonempty.
        /// </summary>
        [HarmonyPrefix]
        [HarmonyPatch(typeof(SaveSystem), nameof(SaveSystem.Remove))]
        private static void RemovePrefix(SaveSlotData slotData, out RemoveState __state)
        {
            __state = RemoveState.Capture(SermonReminderMod.Active, slotData);
        }

        /// <summary>
        /// Advance the process slot generation only for a real successful native
        /// deletion with a valid capture. The native result and execution are
        /// otherwise untouched.
        /// </summary>
        [HarmonyPostfix]
        [HarmonyPatch(typeof(SaveSystem), nameof(SaveSystem.Remove))]
        private static void RemovePostfix(bool __result, RemoveState __state)
        {
            if (__state == null || !__state.Valid || !__result)
                return;

            try { __state.Owner?.HandleSuccessfulDeletion(__state.SlotName, __state.IsDemoSave); }
            catch (Exception) { }
        }

        /// <summary>
        /// Per-invocation deletion capture: the exact owning mod, frozen before native
        /// execution, plus the immutable slot key. Never inspects or deletes files.
        /// </summary>
        internal sealed class RemoveState
        {
            internal SermonReminderMod Owner;
            internal string SlotName;
            internal bool IsDemoSave;
            internal bool Valid;

            internal static RemoveState Capture(SermonReminderMod owner, SaveSlotData slotData)
            {
                var state = new RemoveState { Owner = owner };
                try
                {
                    if (slotData == null)
                        return state;

                    string slotName = slotData.slotName;
                    bool isDemoSave = slotData.isDemoSave;

                    state.SlotName = slotName;
                    state.IsDemoSave = isDemoSave;
                    state.Valid = !string.IsNullOrEmpty(slotName) && owner != null;
                }
                catch (Exception)
                {
                    state.Valid = false;
                }

                return state;
            }
        }
    }
}
