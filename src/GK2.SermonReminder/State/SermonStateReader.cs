using System;

namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Reads a fresh immutable snapshot from the active save. Every field is read
    /// from the live game state on each call; any failure returns an unreadable
    /// snapshot so callers hide the countdown instead of reusing stale values.
    /// </summary>
    internal sealed class SermonStateReader
    {
        internal const string ChurchQuestId = "19_base_ceremony_church";
        internal const string SermonQuestId = "19_base_ceremony_sermon";

        // Read from the game balance at runtime; the sermon weekday is never a
        // hardcoded ordinal.
        private const string SermonWeekdayConst = "day_wrath";

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

                // Gate on the quest prerequisites before any environment or date
                // read. A gate that is not complete is a normal hidden state, so
                // unrelated environment/date problems must not be reported as a
                // fault, nor cause a date read, while the gates are closed.
                bool churchUnlocked = quests.IsQuestInStatus(ChurchQuestId, QuestStatus.Completed);
                bool tutorialCompleted = quests.IsQuestInStatus(SermonQuestId, QuestStatus.Completed);
                if (!(churchUnlocked && tutorialCompleted))
                    return SermonStateSnapshot.Ready(gatesSatisfied: false, delta: 0, daysInWeek: week);

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

                ConstDef sermonDay = ConstDef.Get(SermonWeekdayConst);
                if (sermonDay == null)
                    return SermonStateSnapshot.Unreadable("const '" + SermonWeekdayConst + "' is not defined");

                int sermonWeekday = sermonDay.IntValue;
                if (sermonWeekday < 1 || sermonWeekday > week)
                    return SermonStateSnapshot.Unreadable("sermon weekday out of range: " + sermonWeekday);

                int delta = (sermonWeekday - currentDayNumber + week) % week;
                return SermonStateSnapshot.Ready(gatesSatisfied: true, delta: delta, daysInWeek: week);
            }
            catch (Exception ex)
            {
                return SermonStateSnapshot.Unreadable(ex.GetType().Name);
            }
        }
    }
}
