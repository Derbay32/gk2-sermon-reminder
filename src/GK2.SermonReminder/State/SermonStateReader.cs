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

        // Native daily schedule entries. Their saved execution days distinguish
        // pre-opening and day-end closure without guessing a time or caching state.
        private const string SermonOpenRule = "sermon_ready";
        private const string SermonCloseRule = "sermon_lock";

        internal static SermonStateSnapshot Read(GameSave expectedSave)
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
                        absoluteDay, currentDayNumber, week, delta, SermonDisplayState.Countdown, false);
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

                if (!TryReadSermonWindow(expectedSave, absoluteDay,
                    out bool openedToday, out bool closedToday, out string failure))
                {
                    return SermonStateSnapshot.Unreadable(failure);
                }

                // Both consumption and the native day-end rule clear sermon_ready.
                // After closure we intentionally hide rather than invent a completion
                // history. Before opening, today's HUD reminder is not yet actionable.
                SermonDisplayState display;
                if (closedToday)
                    display = SermonDisplayState.Hidden;
                else if (!openedToday || ready >= 1f)
                    display = SermonDisplayState.Ready;
                else
                    display = SermonDisplayState.Done;

                bool reminderEligible = openedToday && !closedToday && ready >= 1f;
                return SermonStateSnapshot.Resolved(
                    absoluteDay, currentDayNumber, week, 0, display, reminderEligible);
            }
            catch (Exception ex)
            {
                return SermonStateSnapshot.Unreadable(ex.GetType().Name);
            }
        }

        private static bool TryReadSermonWindow(
            GameSave save, int absoluteDay, out bool openedToday, out bool closedToday, out string failure)
        {
            openedToday = false;
            closedToday = false;
            failure = null;

            GameLogicsSystemData logicData = save.gameLogicSystemData;
            if (logicData?.gameLogics == null)
            {
                failure = "game logic system data unavailable";
                return false;
            }

            GameLogicData opening = null;
            GameLogicData closing = null;
            foreach (GameLogicData rule in logicData.gameLogics)
            {
                if (rule == null)
                {
                    failure = "native game logic entry unavailable";
                    return false;
                }

                if (rule.id == SermonOpenRule)
                {
                    if (opening != null)
                    {
                        failure = "duplicate native sermon opening rule";
                        return false;
                    }
                    opening = rule;
                }
                else if (rule.id == SermonCloseRule)
                {
                    if (closing != null)
                    {
                        failure = "duplicate native sermon closing rule";
                        return false;
                    }
                    closing = rule;
                }
            }

            if (opening == null || closing == null)
            {
                failure = "native sermon opening or closing rule unavailable";
                return false;
            }

            int openedDay = opening.lastExecDay;
            int closedDay = closing.lastExecDay;
            // Zero is the native never-executed default. The closing rule cannot
            // legitimately run ahead of the opening rule in this daily schedule.
            if (openedDay < 0 || closedDay < 0 || openedDay > absoluteDay || closedDay > absoluteDay
                || closedDay > openedDay)
            {
                failure = "native sermon rule execution days inconsistent";
                return false;
            }

            openedToday = openedDay == absoluteDay;
            closedToday = closedDay == absoluteDay;
            return true;
        }
    }
}
