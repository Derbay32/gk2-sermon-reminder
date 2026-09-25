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
        internal string Failure { get; }

        private SermonStateSnapshot(
            bool readable,
            bool gatesSatisfied,
            int absoluteDay,
            int dayOfWeek,
            int daysInWeek,
            int delta,
            SermonDisplayState display,
            string failure)
        {
            Readable = readable;
            GatesSatisfied = gatesSatisfied;
            AbsoluteDay = absoluteDay;
            DayOfWeek = dayOfWeek;
            DaysInWeek = daysInWeek;
            Delta = delta;
            Display = display;
            Failure = failure;
        }

        internal static SermonStateSnapshot Unreadable(string failure) =>
            new SermonStateSnapshot(false, false, 0, 0, 0, 0, SermonDisplayState.Hidden, failure);

        /// <summary>Closed quest gates: a normal, non-fault hidden state.</summary>
        internal static SermonStateSnapshot GatesClosed(int daysInWeek) =>
            new SermonStateSnapshot(true, false, 0, 0, daysInWeek, 0, SermonDisplayState.Hidden, null);

        internal static SermonStateSnapshot Resolved(
            int absoluteDay,
            int dayOfWeek,
            int daysInWeek,
            int delta,
            SermonDisplayState display) =>
            new SermonStateSnapshot(true, true, absoluteDay, dayOfWeek, daysInWeek, delta, display, null);
    }
}
