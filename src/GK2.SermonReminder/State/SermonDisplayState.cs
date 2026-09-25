namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Explicit corner-display state derived from the native inputs on every
    /// read. <see cref="Hidden"/> is the normal closed-gate state and is never an
    /// assumed business state after a failed read: an unreadable read yields no
    /// display state at all.
    /// </summary>
    internal enum SermonDisplayState
    {
        /// <summary>Normal hidden state: gates closed, or a non-sermon day.</summary>
        Hidden,

        /// <summary>Gates open, sermon day is still ahead: integer countdown.</summary>
        Countdown,

        /// <summary>Sermon day and native sermon_ready &gt;= 1: the sermon is pending.</summary>
        Ready,

        /// <summary>Sermon day and native sermon_ready &lt; 1: this week's sermon is complete.</summary>
        Done
    }
}
