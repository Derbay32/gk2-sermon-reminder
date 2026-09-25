using System;

namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Reads a fresh immutable snapshot from the active save. Every field is read
    /// from the live game state on each call; any failure returns an unreadable
    /// snapshot so callers hide the display instead of reusing stale values.
    ///
    /// The native quest gates are evaluated first: while either gate is
    /// incomplete the state is normally Hidden and no environment/player read
    /// happens, so an unrelated date problem is never reported as a fault.
    /// </summary>
    internal sealed class SermonStateReader
    {
        internal const string ChurchQuestId = "19_base_ceremony_church";
        internal const string SermonQuestId = "19_base_ceremony_sermon";

        // Read from the game balance at runtime; the sermon weekday is never a
        // hardcoded ordinal.
        private const string SermonWeekdayConst = "day_wrath";

        // Native player resource holding the pending-sermon flag. The accessor's
        // documented default is 0, and an absent key legitimately means 0: the
        // mod never performs a separate key-presence check or copies the state.
        private const string SermonReadyResource = "sermon_ready";

        internal SermonStateSnapshot Read(GameSave expectedSave)
        {
            try
            {
                if (expectedSave == null)
                    return SermonStateSnapshot.Unreadable("no active save");

                QuestSystemData quests = expectedSave.questSystemData;
                if (quests == null)
                    return SermonStateSnapshot.Unreadable("quest system data unavailable");

                int week = EnvironmentData.DAYS_IN_WEEK;

                bool churchUnlocked = quests.IsQuestInStatus(ChurchQuestId, QuestStatus.Completed);
                bool tutorialCompleted = quests.IsQuestInStatus(SermonQuestId, QuestStatus.Completed);
                if (!(churchUnlocked && tutorialCompleted))
                    return SermonStateSnapshot.GatesClosed(week);

                EnvironmentData environment = expectedSave.environmentData;
                if (environment == null)
                    return SermonStateSnapshot.Unreadable("environment data unavailable");

                // EnvironmentData.Day is the absolute day (positive, starts at 1).
                // CurrentDayNumber is only the weekday; without this check an
                // invalid absolute day could masquerade as a valid weekday.
                int absoluteDay = environment.Day;
                if (absoluteDay <= 0)
                    return SermonStateSnapshot.Unreadable("absolute day out of range: " + absoluteDay);

                int currentDayNumber = environment.CurrentDayNumber;
                if (currentDayNumber < 1 || currentDayNumber > week)
                    return SermonStateSnapshot.Unreadable("current day number out of range: " + currentDayNumber);

                // The weekday must be consistent with the absolute day; otherwise the
                // two native reads do not describe one coherent day and no state can
                // be trusted from them.
                int expectedDayNumber = ((absoluteDay - 1) % week) + 1;
                if (currentDayNumber != expectedDayNumber)
                {
                    return SermonStateSnapshot.Unreadable(
                        "day/weekday inconsistency: day=" + absoluteDay + " weekday=" + currentDayNumber);
                }

                ConstDef sermonDay = ConstDef.Get(SermonWeekdayConst);
                if (sermonDay == null)
                    return SermonStateSnapshot.Unreadable("const '" + SermonWeekdayConst + "' is not defined");

                int sermonWeekday = sermonDay.IntValue;
                if (sermonWeekday < 1 || sermonWeekday > week)
                    return SermonStateSnapshot.Unreadable("sermon weekday out of range: " + sermonWeekday);

                int delta = (sermonWeekday - currentDayNumber + week) % week;

                // Off the sermon day the countdown is authoritative regardless of a
                // stale native ready flag left over from the previous cycle; the
                // flag is deliberately not read here.
                if (delta > 0)
                {
                    return SermonStateSnapshot.Resolved(
                        absoluteDay, currentDayNumber, week, delta, SermonDisplayState.Countdown);
                }

                PlayerData player = expectedSave.playerData;
                if (player == null)
                    return SermonStateSnapshot.Unreadable("player data unavailable");

                // Native default 0: an absent key legitimately means "not ready".
                float ready = player.GetRes(SermonReadyResource);
                if (float.IsNaN(ready) || float.IsInfinity(ready))
                {
                    // A non-finite native value is unreadable; never assume Done.
                    return SermonStateSnapshot.Unreadable("native sermon_ready is not finite");
                }

                SermonDisplayState display = ready >= 1f
                    ? SermonDisplayState.Ready
                    : SermonDisplayState.Done;

                return SermonStateSnapshot.Resolved(absoluteDay, currentDayNumber, week, 0, display);
            }
            catch (Exception ex)
            {
                return SermonStateSnapshot.Unreadable(ex.GetType().Name);
            }
        }
    }
}
