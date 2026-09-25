namespace GK2.SermonReminder.State
{
    /// <summary>
    /// Explicit corner-display state derived from the native inputs on every read.
    ///
    /// <see cref="Hidden"/> is the normal hidden state while the quest gates are
    /// closed, and the no-display value carried inside an unreadable snapshot; it is
    /// never an assumed business state after a failed read. With the gates open a
    /// non-sermon day is <see cref="Countdown"/>, never Hidden.
    /// </summary>
    internal enum SermonDisplayState
    {
        /// <summary>Normal hidden state: quest gates closed, or no display at all.</summary>
        Hidden,

        /// <summary>Gates open, sermon day still ahead: integer countdown.</summary>
        Countdown,

        /// <summary>Sermon day and native sermon_ready &gt;= 1: the sermon is pending.</summary>
        Ready,

        /// <summary>Sermon day and native sermon_ready &lt; 1: this week's sermon is complete.</summary>
        Done
    }
}
