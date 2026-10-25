#pragma rtFunctionErrors = 1
#pragma TextEncoding     = "UTF-8"
#pragma rtGlobals        = 3
#pragma version          = 1.00

// ZBR_ProcGlobal.ipf -- ProcGlobal part of the Igor Pro Bridge
//
// The folder of this file is linked into the "User Procedures" folder, and the bridge
// includes this file on demand with INSERTINCLUDE, see ZBR_InsertProcGlobal() in
// ZMQ_BridgeHelpers.ipf. Unlike the ZBR independent module, these functions compile in
// ProcGlobal, so commands and scratch code see the constants, functions and macros of the
// experiment, the same as Igor's own command line.
//
// ZBR_Scratch.ipf is written by the bridge (run_igor_code) and is not part of the repository.

#include "ZBR_Scratch"

/// Deferred: Run the command of the given token in ProcGlobal context, see ZBR_QueueCommand()
/// in ZMQ_BridgeHelpers.ipf
///
/// Execute/Z inside a function creates V_flag as local variable, so no global V_flag is left
/// behind in the current data folder.
Function ZBR_PG_RunCommand(variable idx)

	WAVE/Z/T commandText = root:Packages:ZBR:commandText
	WAVE/Z   errorCode   = root:Packages:ZBR:errorCode

	if(!WaveExists(commandText) || !WaveExists(errorCode) || idx < 0 || idx >= DimSize(commandText, 0))
		return 0
	endif

	Execute/Z commandText[idx]
	errorCode[idx]   = V_flag
	commandText[idx] = ""

	return 0
End

/// Return `exists(name)` and `FunctionInfo(name)` evaluated in ProcGlobal context
Function/S ZBR_PG_SymbolInfo(string name)

	return "EXISTS:" + num2istr(exists(name)) + ";" + FunctionInfo(name)
End
