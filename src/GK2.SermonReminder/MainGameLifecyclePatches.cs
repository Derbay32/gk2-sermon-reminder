using HarmonyLib;

namespace GK2.SermonReminder
{
    /// <summary>
    /// Early Harmony prefixes on the native load / menu transitions. They run
    /// before the original methods finish, so readiness is dropped and the old
    /// display cleared before any new save data becomes authoritative. Only our
    /// own patches are installed and removed.
    /// </summary>
    internal static class MainGameLifecyclePatches
    {
        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.ContinueGame))]
        private static void ContinueGamePrefix() => SermonReminderMod.Active?.HandleNativeLoadStarting();

        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.CreateGameSaveAndStart))]
        private static void CreateGameSaveAndStartPrefix() => SermonReminderMod.Active?.HandleNativeLoadStarting();

        [HarmonyPrefix]
        [HarmonyPatch(typeof(MainGame), nameof(MainGame.GoToMenu))]
        private static void GoToMenuPrefix() => SermonReminderMod.Active?.HandleReturnedToMenu();
    }
}
