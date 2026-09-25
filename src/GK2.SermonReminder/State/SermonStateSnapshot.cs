namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Immutable per-tick view of the native countdown inputs. A failed read
    /// yields an unreadable snapshot; it never carries an assumed business state.
    /// </summary>
    internal readonly struct SermonStateSnapshot
    {
        internal bool Readable { get; }
        internal bool GatesSatisfied { get; }
        internal int Delta { get; }
        internal int DaysInWeek { get; }
        internal string Failure { get; }

        private SermonStateSnapshot(bool readable, bool gatesSatisfied, int delta, int daysInWeek, string failure)
        {
            Readable = readable;
            GatesSatisfied = gatesSatisfied;
            Delta = delta;
            DaysInWeek = daysInWeek;
            Failure = failure;
        }

        internal static SermonStateSnapshot Unreadable(string failure) =>
            new SermonStateSnapshot(false, false, 0, 0, failure);

        internal static SermonStateSnapshot Ready(bool gatesSatisfied, int delta, int daysInWeek) =>
            new SermonStateSnapshot(true, gatesSatisfied, delta, daysInWeek, null);
    }
}
