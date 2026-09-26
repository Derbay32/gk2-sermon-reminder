using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using LazyBearTechnology;

namespace GK2.SermonReminder.Popup
{
    /// <summary>
    /// Narrow, opt-in provenance capture for the one native acquisition that
    /// creates each owned dialog button.
    ///
    /// The native <c>UIDialogWindow.Open(UIDialogWindowData)</c> override recycles
    /// its prior buttons and then acquires exactly one pool button per request. The
    /// mod cannot infer ownership from window membership or from a pre-claim: a
    /// native Close leaves old foreign buttons in the frozen active list, and a
    /// failed Open can return before it touches them. This helper therefore records
    /// the acquisition itself. A transpiler inserts one observer call immediately
    /// after the single exact <c>Pool.GetOrCreateObject&lt;UIDialogWindowButton&gt;</c>
    /// call site, and a bounded per-thread scope forwards the exact returned
    /// reference only while the mod's own <c>Open</c> is on the stack.
    ///
    /// The module fails closed. If the exact call site cannot be identified, if the
    /// insertion point is not provably safe (inside or adjacent to an exception
    /// region, or split from an IL prefix), or if the transpiler cannot run, it
    /// registers a no-op and reports unsupported: the popup then refuses to
    /// pre-claim or open rather than guess ownership. Insertion safety is judged
    /// locally around the unique acquisition, so unrelated closed or later
    /// exception regions elsewhere in the current native method are allowed.
    /// Nothing here mutates Unity state, discovers list/pool membership, or
    /// replaces the native method.
    /// </summary>
    internal static class SermonPopupButtonCapture
    {
        private const string AcquisitionMethodName = "GetOrCreateObject";

        // The exact instrumented method: the ONE-argument native override, never the
        // inherited generic wrapper the popup calls.
        private static readonly MethodInfo TargetMethod = ResolveTargetMethod();

        private static readonly MethodInfo ObserverMethod =
            AccessTools.Method(typeof(SermonPopupButtonCapture), nameof(ObserveAcquisition));

        private static readonly MethodInfo TranspilerMethod =
            AccessTools.Method(typeof(SermonPopupButtonCapture), nameof(Transpiler));

        // Updated on EVERY transpiler run, so a replacement rebuilt because another
        // mod patched the same method cannot leave a stale success behind.
        private static volatile bool patternSupported;

        // The plugin's existing Harmony owner, assigned once after OnEnable creates
        // it and cleared on teardown. Never a second Harmony id.
        private static Harmony ownerHarmony;

        internal static void ConfigureHarmony(Harmony harmony) => ownerHarmony = harmony;

        internal static void ClearConfiguration()
        {
            ownerHarmony = null;
            patternSupported = false;
        }

        /// <summary>
        /// Verify that the exact native call site is instrumented by this owner right
        /// now, installing the transpiler on first use. Registration is re-read from
        /// Harmony's live patch table every time, so an unpatch/re-enable cycle can
        /// never trust a stale success flag.
        /// </summary>
        internal static bool EnsureReady(out string detail)
        {
            if (!EnsureInstalled(out detail))
                return false;

            if (!patternSupported)
            {
                detail = "pattern-unsupported";
                return false;
            }

            return true;
        }

        private static bool EnsureInstalled(out string detail)
        {
            detail = null;

            if (TargetMethod == null)
            {
                detail = "target-missing";
                return false;
            }

            if (TranspilerMethod == null)
            {
                detail = "transpiler-missing";
                return false;
            }

            if (ObserverMethod == null)
            {
                detail = "observer-missing";
                return false;
            }

            if (IsRegistered())
                return true;

            Harmony harmony = ownerHarmony;
            if (harmony == null)
            {
                detail = "harmony-unconfigured";
                return false;
            }

            try
            {
                harmony.Patch(TargetMethod, transpiler: new HarmonyMethod(TranspilerMethod));
            }
            catch (Exception ex)
            {
                detail = "install:" + ex.GetType().Name;
                return false;
            }

            if (!IsRegistered())
            {
                detail = "not-registered";
                return false;
            }

            return true;
        }

        private static bool IsRegistered()
        {
            try
            {
                Patches patches = Harmony.GetPatchInfo(TargetMethod);
                if (patches == null)
                    return false;

                foreach (Patch patch in patches.Transpilers)
                {
                    if (patch == null)
                        continue;
                    if (!string.Equals(patch.owner, SermonReminderPlugin.PluginGuid, StringComparison.Ordinal))
                        continue;
                    if (SameMethod(patch.PatchMethod, TranspilerMethod))
                        return true;
                }

                return false;
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>
        /// Metadata-level method identity. Mono does not guarantee that two reflection
        /// descriptors for the same method are the same object, so registration matches
        /// on module and metadata token instead of object identity. The caller already
        /// restricts this to the plugin's own owner id.
        /// </summary>
        private static bool SameMethod(MethodInfo left, MethodInfo right)
        {
            if (ReferenceEquals(left, right))
                return true;
            if (left == null || right == null)
                return false;

            return left.Module == right.Module && left.MetadataToken == right.MetadataToken;
        }

        /// <summary>
        /// Open a bounded capture scope around one native Open call. The observer is a
        /// no-op unless the native window and data are the exact reference-equal
        /// arguments bound here, so a nested or foreign Open is never captured. The
        /// scope is restored to its predecessor on Dispose, including when Open
        /// throws.
        /// </summary>
        internal static CaptureScope BeginScope(
            UIDialogWindow window,
            UIDialogWindowData data,
            Action<UIDialogWindowButton> record)
            => new CaptureScope(window, data, record);

        // --- transpiler --------------------------------------------------------------

        private static IEnumerable<CodeInstruction> Transpiler(IEnumerable<CodeInstruction> instructions)
        {
            List<CodeInstruction> input = null;
            try
            {
                input = instructions as List<CodeInstruction>;
                if (input == null)
                {
                    input = new List<CodeInstruction>();
                    foreach (CodeInstruction instruction in instructions)
                        input.Add(instruction);
                }
            }
            catch (Exception)
            {
                MarkUnsupported();
                return instructions;
            }

            try
            {
                if (!TryFindAcquisition(input, out int acquisitionIndex))
                {
                    MarkUnsupported();
                    return input;
                }

                int nextIndex = acquisitionIndex + 1;

                // The insertion must be locally provable: the acquisition needs a
                // following instruction to host the capture before its labels, the
                // insertion point must sit outside every exception region, and the
                // observer target must resolve. Unrelated closed or later exception
                // regions elsewhere in the method are allowed.
                if (nextIndex >= input.Count || ObserverMethod == null || !IsSafeInsertionPoint(input, acquisitionIndex))
                {
                    MarkUnsupported();
                    return input;
                }

                var output = new List<CodeInstruction>(input.Count + 4);
                for (int i = 0; i < input.Count; i++)
                {
                    output.Add(input[i]);
                    if (i != acquisitionIndex)
                        continue;

                    // The acquisition return value is on the stack. Duplicate it and
                    // pass it with the exact window/data arguments to the observer; the
                    // original return stays on the stack, so the native IL is
                    // otherwise untouched. No labels or exception blocks are moved or
                    // duplicated onto these instructions, so jumps that target the
                    // following instruction still bypass the capture.
                    output.Add(new CodeInstruction(OpCodes.Dup));
                    output.Add(new CodeInstruction(OpCodes.Ldarg_0));
                    output.Add(new CodeInstruction(OpCodes.Ldarg_1));
                    output.Add(new CodeInstruction(OpCodes.Call, ObserverMethod));
                }

                MarkSupported();
                return output;
            }
            catch (Exception)
            {
                MarkUnsupported();
                return input;
            }
        }

        private static bool TryFindAcquisition(List<CodeInstruction> input, out int index)
        {
            index = -1;
            int count = 0;

            for (int i = 0; i < input.Count; i++)
            {
                if (!IsAcquisition(input[i]))
                    continue;

                count++;
                index = i;
            }

            return count == 1;
        }

        private static bool IsAcquisition(CodeInstruction instruction)
        {
            if (instruction == null)
                return false;
            if (instruction.opcode != OpCodes.Call && instruction.opcode != OpCodes.Callvirt)
                return false;
            if (!(instruction.operand is MethodInfo method))
                return false;
            if (!string.Equals(method.Name, AcquisitionMethodName, StringComparison.Ordinal))
                return false;
            if (method.DeclaringType != typeof(Pool))
                return false;
            if (method.IsStatic || !method.IsGenericMethod || method.IsGenericMethodDefinition)
                return false;
            if (method.GetParameters().Length != 0)
                return false;
            if (method.ReturnType != typeof(UIDialogWindowButton))
                return false;

            Type[] typeArguments = method.GetGenericArguments();
            return typeArguments.Length == 1 && typeArguments[0] == typeof(UIDialogWindowButton);
        }

        /// <summary>
        /// True when the observer may be appended immediately after the acquisition
        /// without moving or duplicating any original label or exception block. The
        /// insertion point must be at exception-handling depth zero, outside any block
        /// boundary, and not immediately preceded by an IL prefix. Only metadata up to
        /// the acquisition is inspected, so closed regions before it and unrelated
        /// regions after it stay permitted.
        /// </summary>
        private static bool IsSafeInsertionPoint(List<CodeInstruction> input, int acquisitionIndex)
        {
            int depth = 0;
            for (int i = 0; i < acquisitionIndex; i++)
            {
                CodeInstruction instruction = input[i];
                if (instruction == null || instruction.blocks == null)
                    continue;

                foreach (ExceptionBlock block in instruction.blocks)
                {
                    if (block == null)
                        continue;

                    switch (block.blockType)
                    {
                        case ExceptionBlockType.BeginExceptionBlock:
                            depth++;
                            break;
                        case ExceptionBlockType.EndExceptionBlock:
                            depth--;
                            if (depth < 0)
                                return false; // Malformed: an end without a begin.
                            break;
                        default:
                            // Catch/finally/filter/fault transitions stay inside the
                            // enclosing region and never change nesting depth.
                            break;
                    }
                }
            }

            if (depth != 0)
                return false; // The acquisition sits inside an open protected region.

            // A block boundary on the acquisition or on the instruction that will
            // follow the inserted sequence would leave the original metadata
            // ambiguous, so neither is a valid host.
            if (HasBlockMetadata(input[acquisitionIndex]) || HasBlockMetadata(input[acquisitionIndex + 1]))
                return false;

            // An IL prefix (tail./constrained./readonly./volatile./unaligned./no.)
            // applies to the instruction that follows it; never split that pair.
            return acquisitionIndex == 0 || !IsPrefix(input[acquisitionIndex - 1]);
        }

        private static bool HasBlockMetadata(CodeInstruction instruction)
            => instruction != null && instruction.blocks != null && instruction.blocks.Count > 0;

        private static bool IsPrefix(CodeInstruction instruction)
            => instruction != null && instruction.opcode.OpCodeType == OpCodeType.Prefix;

        private static void MarkSupported() => patternSupported = true;

        private static void MarkUnsupported() => patternSupported = false;

        // --- observer ----------------------------------------------------------------

        /// <summary>
        /// Harmony-injected observer. It must never disturb native execution, so it is
        /// fully contained and does nothing outside the exact owned scope.
        /// </summary>
        private static void ObserveAcquisition(
            UIDialogWindowButton button,
            UIDialogWindow window,
            UIDialogWindowData data)
        {
            CaptureScope scope = null;
            try
            {
                scope = CaptureScope.Current;
                if (scope == null || !scope.Matches(window, data))
                    return;

                scope.Observe(button);
            }
            catch (Exception)
            {
                try { scope?.MarkRecordFailed(); }
                catch (Exception) { }
            }
        }

        private static MethodInfo ResolveTargetMethod()
        {
            try
            {
                MethodInfo method = AccessTools.DeclaredMethod(
                    typeof(UIDialogWindow),
                    "Open",
                    new[] { typeof(UIDialogWindowData) });

                if (method == null)
                    return null;
                if (method.IsStatic || method.ReturnType != typeof(void))
                    return null;
                if (method.DeclaringType != typeof(UIDialogWindow))
                    return null;

                ParameterInfo[] parameters = method.GetParameters();
                if (parameters.Length != 1 || parameters[0].ParameterType != typeof(UIDialogWindowData))
                    return null;

                return method;
            }
            catch (Exception)
            {
                return null;
            }
        }

        // --- capture scope -----------------------------------------------------------

        /// <summary>
        /// Per-thread ownership observation window, bound to the exact native window
        /// and data references of one owned Open. Only the exact returned button
        /// reference is forwarded to the transaction; no Unity state is read or
        /// mutated and no list/parent/pool membership is inspected. A recording
        /// failure is retained on the scope so the transaction can quarantine the
        /// incomplete capture instead of inventing ownership.
        /// </summary>
        internal sealed class CaptureScope : IDisposable
        {
            [ThreadStatic]
            private static CaptureScope current;

            internal static CaptureScope Current => current;

            private readonly CaptureScope previous;
            private readonly UIDialogWindow window;
            private readonly UIDialogWindowData data;
            private readonly Action<UIDialogWindowButton> record;
            private bool disposed;

            internal CaptureScope(UIDialogWindow window, UIDialogWindowData data, Action<UIDialogWindowButton> record)
            {
                this.window = window;
                this.data = data;
                this.record = record;
                previous = current;
                current = this;
            }

            internal bool RecordFailed { get; private set; }

            internal bool Matches(UIDialogWindow candidateWindow, UIDialogWindowData candidateData)
                => ReferenceEquals(window, candidateWindow) && ReferenceEquals(data, candidateData);

            internal void Observe(UIDialogWindowButton button)
            {
                try
                {
                    record?.Invoke(button);
                }
                catch (Exception)
                {
                    RecordFailed = true;
                }
            }

            internal void MarkRecordFailed() => RecordFailed = true;

            public void Dispose()
            {
                if (disposed)
                    return;
                disposed = true;

                if (ReferenceEquals(current, this))
                    current = previous;
            }
        }
    }
}
