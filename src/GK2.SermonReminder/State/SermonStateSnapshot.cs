namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Immutable per-tick view of the native state inputs. A failed read yields an
    /// unreadable snapshot carrying no display state, so callers can never reuse a
    /// stale Ready/Done judgement after the native read failed.
    /// </summary>
    internal readonly struct SermonStateSnapshot
    {
        internal bool Readable { get; }
        internal bool GatesSatisfied { get; }
        internal int AbsoluteDay { get; }
        internal int DayOfWeek { get; }
        internal int DaysInWeek { get; }
        internal int Delta { get; }
        internal SermonDisplayState Display { get; }

        /// <summary>
        /// Native opportunity is open and unconsumed. Ready text alone does not
        /// imply this: the HUD also shows Ready before the native opening rule runs.
        /// Per-load toggles and popup safety/dedup gates are applied by consumers.
        /// </summary>
        internal bool ReminderEligible { get; }
        internal string Failure { get; }

        private SermonStateSnapshot(
            bool readable,
            bool gatesSatisfied,
            int absoluteDay,
            int dayOfWeek,
            int daysInWeek,
            int delta,
            SermonDisplayState display,
            bool reminderEligible,
            string failure)
        {
            Readable = readable;
            GatesSatisfied = gatesSatisfied;
            AbsoluteDay = absoluteDay;
            DayOfWeek = dayOfWeek;
            DaysInWeek = daysInWeek;
            Delta = delta;
            Display = display;
            ReminderEligible = reminderEligible;
            Failure = failure;
        }

        internal static SermonStateSnapshot Unreadable(string failure) =>
            new SermonStateSnapshot(false, false, 0, 0, 0, 0, SermonDisplayState.Hidden, false, failure);

        /// <summary>Closed quest gates: a normal, non-fault hidden state.</summary>
        internal static SermonStateSnapshot GatesClosed(int daysInWeek) =>
            new SermonStateSnapshot(true, false, 0, 0, daysInWeek, 0, SermonDisplayState.Hidden, false, null);

        internal static SermonStateSnapshot Resolved(
            int absoluteDay,
            int dayOfWeek,
            int daysInWeek,
            int delta,
            SermonDisplayState display,
            bool reminderEligible) =>
            new SermonStateSnapshot(true, true, absoluteDay, dayOfWeek, daysInWeek, delta, display, reminderEligible, null);
    }
}
