
/*! @file
 *  This is an example of the PIN tool that demonstrates some basic PIN APIs
 *  and could serve as the starting point for developing your first PIN tool
 */

#include "pin.H"
#include <fstream>
#include <iostream>
#include <stdlib.h>
#include <string.h>
#include <string>

using namespace std;

#define NUM_INSTR_DESTINATIONS 2
#define NUM_INSTR_SOURCES 4

typedef struct trace_instr_format {
  unsigned long long int ip; // instruction pointer (program counter) value

  unsigned char is_branch;    // is this branch
  unsigned char branch_taken; // if so, is this taken

  unsigned char
      destination_registers[NUM_INSTR_DESTINATIONS]; // output registers
  unsigned char source_registers[NUM_INSTR_SOURCES]; // input registers

  unsigned long long int
      destination_memory[NUM_INSTR_DESTINATIONS];          // output memory
  unsigned long long int source_memory[NUM_INSTR_SOURCES]; // input memory
} trace_instr_format_t;

/* ================================================================== */
// Global variables
/* ================================================================== */

UINT64 instrCount = 0;
UINT64 writtenCount = 0;
bool roi_active = false;
FILE *phase_out = NULL;

FILE *out;

bool output_file_closed = false;
bool tracing_on = false;

trace_instr_format_t curr_instr;

/* ===================================================================== */
// Command line switches
/* ===================================================================== */
KNOB<string> KnobOutputFile(KNOB_MODE_WRITEONCE, "pintool", "o",
                            "champsim.trace",
                            "specify file name for Champsim tracer output");

KNOB<UINT64>
    KnobSkipInstructions(KNOB_MODE_WRITEONCE, "pintool", "s", "0",
                         "How many instructions to skip before tracing begins");

KNOB<UINT64> KnobTraceInstructions(KNOB_MODE_WRITEONCE, "pintool", "t",
                                   "1000000", "How many instructions to trace");
KNOB<BOOL> KnobROI(KNOB_MODE_WRITEONCE, "pintool", "roi", "0",
                   "Trace only between PrefetchSteer ROI markers (single thread)");
KNOB<string> KnobPhases(KNOB_MODE_WRITEONCE, "pintool", "phases", "",
                        "CSV of ROI/phase boundaries in emitted trace records");

/* ===================================================================== */
// Utilities
/* ===================================================================== */

/*!
 *  Print out help message.
 */
INT32 Usage() {
  cerr << "This tool creates a register and memory access trace" << endl
       << "Specify the output trace file with -o" << endl
       << "Specify the number of instructions to skip before tracing with -s"
       << endl
       << "Specify the number of instructions to trace with -t" << endl
       << endl;

  cerr << KNOB_BASE::StringKnobSummary() << endl;

  return -1;
}

/* ===================================================================== */
// Analysis routines
/* ===================================================================== */

void RecordPhase(const char *event, UINT64 phase = 0, UINT64 pulse = 0) {
  if (phase_out && fprintf(phase_out, "%s,%llu,%llu,%llu\n", event,
                           (unsigned long long)writtenCount,
                           (unsigned long long)phase,
                           (unsigned long long)pulse) < 0) {
    cerr << "Failed to write phase log" << endl;
    PIN_ExitProcess(1);
  }
}

void CloseOutput(const char *reason) {
  if (output_file_closed) return;
  RecordPhase(reason);
  bool failed = fclose(out) != 0;
  output_file_closed = true;
  if (phase_out) {
    failed = (fclose(phase_out) != 0) || failed;
    phase_out = NULL;
  }
  if (failed) {
    cerr << "Failed to close trace output" << endl;
    PIN_ExitProcess(1);
  }
}

void ROIBegin() {
  roi_active = true;
  RecordPhase("roi_begin");
}

void ROIEnd() {
  roi_active = false;
  tracing_on = false;
  CloseOutput("roi_end");
}

void Phase(ADDRINT phase, ADDRINT pulse) {
  if (roi_active && !output_file_closed) RecordPhase("phase", phase, pulse);
}

VOID Image(IMG img, VOID *) {
  if (!KnobROI.Value() || !IMG_IsMainExecutable(img)) return;
  RTN begin = RTN_FindByName(img, "prefetchsteer_roi_begin");
  RTN end = RTN_FindByName(img, "prefetchsteer_roi_end");
  RTN phase = RTN_FindByName(img, "prefetchsteer_phase");
  if (!RTN_Valid(begin) || !RTN_Valid(end) || !RTN_Valid(phase)) {
    cerr << "-roi requires an unstripped PrefetchSteer executable" << endl;
    PIN_ExitProcess(1);
  }
  RTN_Open(begin);
  RTN_InsertCall(begin, IPOINT_BEFORE, (AFUNPTR)ROIBegin, IARG_END);
  RTN_Close(begin);
  RTN_Open(end);
  RTN_InsertCall(end, IPOINT_BEFORE, (AFUNPTR)ROIEnd, IARG_END);
  RTN_Close(end);
  RTN_Open(phase);
  RTN_InsertCall(phase, IPOINT_BEFORE, (AFUNPTR)Phase,
                 IARG_FUNCARG_ENTRYPOINT_VALUE, 0,
                 IARG_FUNCARG_ENTRYPOINT_VALUE, 1, IARG_END);
  RTN_Close(phase);
}

void BeginInstruction(VOID *ip, UINT32 op_code) {
  tracing_on = false;
  if (output_file_closed || (KnobROI.Value() && !roi_active)) return;
  instrCount++;
  // printf("[%p %u %s ", ip, opcode, (char*)opstring);

  if (instrCount > KnobSkipInstructions.Value()) {
    tracing_on = true;

    if (instrCount - KnobSkipInstructions.Value() > KnobTraceInstructions.Value()) {
      tracing_on = false;
      CloseOutput("trace_limit");
      PIN_ExitApplication(0);
    }
  }

  if (!tracing_on)
    return;

  // reset the current instruction
  curr_instr.ip = (unsigned long long int)ip;

  curr_instr.is_branch = 0;
  curr_instr.branch_taken = 0;

  for (int i = 0; i < NUM_INSTR_DESTINATIONS; i++) {
    curr_instr.destination_registers[i] = 0;
    curr_instr.destination_memory[i] = 0;
  }

  for (int i = 0; i < NUM_INSTR_SOURCES; i++) {
    curr_instr.source_registers[i] = 0;
    curr_instr.source_memory[i] = 0;
  }
}

void EndInstruction() {
  // printf("%d]\n", (int)instrCount);

  // printf("\n");

  if (tracing_on && !output_file_closed) {
    if (fwrite(&curr_instr, sizeof(trace_instr_format_t), 1, out) != 1) {
      cerr << "Failed to write trace" << endl;
      PIN_ExitProcess(1);
    }
    ++writtenCount;
  }
}

void BranchOrNot(UINT32 taken) {
  // printf("[%d] ", taken);

  curr_instr.is_branch = 1;
  if (taken != 0) {
    curr_instr.branch_taken = 1;
  }
}

void RegRead(UINT32 i, UINT32 index) {
  if (!tracing_on)
    return;

  REG r = (REG)i;

  /*
     if(r == 26)
     {
  // 26 is the IP, which is read and written by branches
  return;
  }
  */

  // cout << r << " " << REG_StringShort((REG)r) << " " ;
  // cout << REG_StringShort((REG)r) << " " ;

  // printf("%d ", (int)r);

  // check to see if this register is already in the list
  int already_found = 0;
  for (int i = 0; i < NUM_INSTR_SOURCES; i++) {
    if (curr_instr.source_registers[i] == ((unsigned char)r)) {
      already_found = 1;
      break;
    }
  }
  if (already_found == 0) {
    for (int i = 0; i < NUM_INSTR_SOURCES; i++) {
      if (curr_instr.source_registers[i] == 0) {
        curr_instr.source_registers[i] = (unsigned char)r;
        break;
      }
    }
  }
}

void RegWrite(REG i, UINT32 index) {
  if (!tracing_on)
    return;

  REG r = (REG)i;

  /*
     if(r == 26)
     {
  // 26 is the IP, which is read and written by branches
  return;
  }
  */

  // cout << "<" << r << " " << REG_StringShort((REG)r) << "> ";
  // cout << "<" << REG_StringShort((REG)r) << "> ";

  // printf("<%d> ", (int)r);

  int already_found = 0;
  for (int i = 0; i < NUM_INSTR_DESTINATIONS; i++) {
    if (curr_instr.destination_registers[i] == ((unsigned char)r)) {
      already_found = 1;
      break;
    }
  }
  if (already_found == 0) {
    for (int i = 0; i < NUM_INSTR_DESTINATIONS; i++) {
      if (curr_instr.destination_registers[i] == 0) {
        curr_instr.destination_registers[i] = (unsigned char)r;
        break;
      }
    }
  }
  /*
     if(index==0)
     {
     curr_instr.destination_register = (unsigned long long int)r;
     }
     */
}

void MemoryRead(VOID *addr, UINT32 index, UINT32 read_size) {
  if (!tracing_on)
    return;

  // printf("0x%llx,%u ", (unsigned long long int)addr, read_size);

  // check to see if this memory read location is already in the list
  int already_found = 0;
  for (int i = 0; i < NUM_INSTR_SOURCES; i++) {
    if (curr_instr.source_memory[i] == ((unsigned long long int)addr)) {
      already_found = 1;
      break;
    }
  }
  if (already_found == 0) {
    for (int i = 0; i < NUM_INSTR_SOURCES; i++) {
      if (curr_instr.source_memory[i] == 0) {
        curr_instr.source_memory[i] = (unsigned long long int)addr;
        break;
      }
    }
  }
}

void MemoryWrite(VOID *addr, UINT32 index) {
  if (!tracing_on)
    return;

  // printf("(0x%llx) ", (unsigned long long int) addr);

  // check to see if this memory write location is already in the list
  int already_found = 0;
  for (int i = 0; i < NUM_INSTR_DESTINATIONS; i++) {
    if (curr_instr.destination_memory[i] == ((unsigned long long int)addr)) {
      already_found = 1;
      break;
    }
  }
  if (already_found == 0) {
    for (int i = 0; i < NUM_INSTR_DESTINATIONS; i++) {
      if (curr_instr.destination_memory[i] == 0) {
        curr_instr.destination_memory[i] = (unsigned long long int)addr;
        break;
      }
    }
  }
  /*
     if(index==0)
     {
     curr_instr.destination_memory = (long long int)addr;
     }
     */
}

/* ===================================================================== */
// Instrumentation callbacks
/* ===================================================================== */

// Is called for every instruction and instruments reads and writes
VOID Instruction(INS ins, VOID *v) {
  // begin each instruction with this function
  UINT32 opcode = INS_Opcode(ins);
  INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)BeginInstruction, IARG_INST_PTR,
                 IARG_UINT32, opcode, IARG_END);

  // instrument branch instructions
  if (INS_IsBranch(ins) && !INS_IsXbegin(ins) && !INS_IsXend(ins))
    INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)BranchOrNot, IARG_BRANCH_TAKEN,
                   IARG_END);

  // instrument register reads
  UINT32 readRegCount = INS_MaxNumRRegs(ins);
  for (UINT32 i = 0; i < readRegCount; i++) {
    UINT32 regNum = INS_RegR(ins, i);

    INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)RegRead, IARG_UINT32, regNum,
                   IARG_UINT32, i, IARG_END);
  }

  // instrument register writes
  UINT32 writeRegCount = INS_MaxNumWRegs(ins);
  for (UINT32 i = 0; i < writeRegCount; i++) {
    UINT32 regNum = INS_RegW(ins, i);

    INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)RegWrite, IARG_UINT32, regNum,
                   IARG_UINT32, i, IARG_END);
  }

  // instrument memory reads and writes
  UINT32 memOperands = INS_MemoryOperandCount(ins);

  // Iterate over each memory operand of the instruction.
  for (UINT32 memOp = 0; memOp < memOperands; memOp++) {
    if (INS_MemoryOperandIsRead(ins, memOp)) {
      UINT32 read_size = INS_MemoryOperandSize(ins, memOp);

      INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)MemoryRead, IARG_MEMORYOP_EA,
                     memOp, IARG_UINT32, memOp, IARG_UINT32, read_size,
                     IARG_END);
    }
    if (INS_MemoryOperandIsWritten(ins, memOp)) {
      INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)MemoryWrite, IARG_MEMORYOP_EA,
                     memOp, IARG_UINT32, memOp, IARG_END);
    }
  }

  // finalize each instruction with this function
  INS_InsertCall(ins, IPOINT_BEFORE, (AFUNPTR)EndInstruction, IARG_END);
}

/*!
 * Print out analysis results.
 * This function is called when the application exits.
 * @param[in]   code            exit code of the application
 * @param[in]   v               value specified by the tool in the
 *                              PIN_AddFiniFunction function call
 */
VOID Fini(INT32 code, VOID *v) {
  // close the file if it hasn't already been closed
  CloseOutput("program_end");
}

/*!
 * The main procedure of the tool.
 * This function is called when the application image is loaded but not yet
 * started.
 * @param[in]   argc            total number of elements in the argv array
 * @param[in]   argv            array of command line arguments,
 *                              including pin -t <toolname> -- ...
 */
int main(int argc, char *argv[]) {
  PIN_InitSymbols();
  // Initialize PIN library. Print help message if -h(elp) is specified
  // in the command line or the command line is invalid
  if (PIN_Init(argc, argv))
    return Usage();

  const string fileName = KnobOutputFile.Value();
  out = fopen(fileName.c_str(), "wb");
  if (!out) {
    cout << "Couldn't open output trace file. Exiting." << endl;
    exit(1);
  }
  if (!KnobTraceInstructions.Value()) {
    cerr << "Trace instruction limit must be positive" << endl;
    fclose(out);
    return 1;
  }
  if (!KnobPhases.Value().empty()) {
    if (KnobPhases.Value() == fileName) {
      cerr << "Trace and phase log must use different paths" << endl;
      fclose(out);
      return 1;
    }
    phase_out = fopen(KnobPhases.Value().c_str(), "w");
    if (!phase_out) {
      cerr << "Couldn't open phase log" << endl;
      fclose(out);
      return 1;
    }
    fprintf(phase_out, "event,trace_instruction,phase,pulse\n");
  }
  IMG_AddInstrumentFunction(Image, 0);

  // Register function to be called to instrument instructions
  INS_AddInstrumentFunction(Instruction, 0);

  // Register function to be called when the application exits
  PIN_AddFiniFunction(Fini, 0);

  // cerr <<  "===============================================" << endl;
  // cerr <<  "This application is instrumented by the Champsim Trace Generator"
  // << endl; cerr <<  "Trace saved in " << KnobOutputFile.Value() << endl; cerr
  // <<  "===============================================" << endl;

  // Start the program, never returns
  PIN_StartProgram();

  return 0;
}

/* ===================================================================== */
/* eof */
/* ===================================================================== */
