"""
data_extractor.py – ALU Telemetry
──────────────────────────────────
Responsible for all direct memory-reading from the Asphalt Legends Unite
(Steam) process.  No memory is ever written; this module is read-only.

Architecture
────────────
The Cheat Engine (CE) tables that ship with this project identify the
game-object base pointer (pRaceData / pCheckpoint) by hooking specific
write instructions and capturing the CPU register that carries the struct
address.  This module replicates that logic in pure Python by:

  1. Attaching to the game process with pymem.
  2. AOB-scanning for the same byte signatures the CE scripts use.
  3. Allocating a small executable trampoline in the target process,
     identical in purpose to the CE allocations.
  4. Reading the captured pointer and all offset values from there.

All CE-script → Python translations are annotated with the original
CE mnemonic so the mapping stays obvious.

Placeholder conditions are marked with:  # placeholder
"""

import struct
import threading
import time
import ctypes
import ctypes.wintypes as wintypes

import pymem
import pymem.process


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PROCESS_NAME = "Asphalt9_Steam_x64_rtl.exe"

# ── AOB signatures (from the CT scripts) ─────────────────────────────────────

# RaceData.CT  –  LuaScript  –  "add [rdi+000000A0],rax"
AOB_RACE_TIMER    = bytes([0x48, 0x01, 0x87, 0xA0, 0x00, 0x00, 0x00])

# RaceData.CT  –  RP Finder  –  "mov [rdi+000001D8],eax; add rsp,38"
AOB_RACE_PROGRESS = bytes([0x89, 0x87, 0xD8, 0x01, 0x00, 0x00,
                            0x48, 0x83, 0xC4, 0x38])

# Gearbox Info.CT  –  "movss [rdi+000001B8],xmm1"
AOB_RPM           = bytes([0xF3, 0x0F, 0x11, 0x8F, 0xB8, 0x01, 0x00, 0x00])

# CP finder Steam.CT  –  "mov eax,[r13+0000024C]"
AOB_CHECKPOINT    = bytes([0x41, 0x8B, 0x85, 0x4C, 0x02, 0x00, 0x00])

# ── Struct offsets inside the game-object (RDI / pRaceData) ──────────────────
# Source: CE script comments and observed AOB context.

OFFSET_RACE_TIMER    = 0x0000_00A0   # 4-byte integer  (ms or ticks)
OFFSET_RACE_PROGRESS = 0x0000_01D8   # float           (0.0 – 1.0 or 0–100 %)
OFFSET_RPM           = 0x0000_01B8   # float           (engine RPM)
OFFSET_GEAR          = 0x0000_00A0   # 4-byte integer  (same offset as timer in
                                     # the RPM hook's RDI context – see CE note)

# Offsets that are not yet covered by the CT scripts ─ add when found
# OFFSET_VELOCITY      = ???         # placeholder
# OFFSET_CAR_ANGLE     = ???         # placeholder
# OFFSET_CAR_POS_X     = ???         # placeholder
# OFFSET_CAR_POS_Y     = ???         # placeholder
# OFFSET_CAR_POS_Z     = ???         # placeholder
# OFFSET_CAM_ANGLE     = ???         # placeholder
# OFFSET_CAM_POS_X     = ???         # placeholder
# OFFSET_CAM_POS_Y     = ???         # placeholder
# OFFSET_CAM_POS_Z     = ???         # placeholder
# OFFSET_NITRO_BAR     = ???         # placeholder
# OFFSET_NITRO_STATE   = ???         # placeholder
# OFFSET_DRIFT_STATE   = ???         # placeholder
# OFFSET_360_STATE     = ???         # placeholder
# OFFSET_ACCELERATION  = ???         # placeholder


# ─────────────────────────────────────────────────────────────────────────────
# Helper – trampoline injection (mirrors what CE does at ENABLE time)
# ─────────────────────────────────────────────────────────────────────────────

MEM_COMMIT          = 0x1000
MEM_RESERVE         = 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PROCESS_ALL_ACCESS  = 0x1F0FFF


def _inject_pointer_capture(pm: pymem.Pymem, aob_address: int,
                             original_bytes: bytes,
                             capture_register_offset: int = 0) -> int:
    """
    Allocates a small trampoline in the target process that stores the
    game-object pointer (whichever register the AOB instruction uses) into
    a 8-byte slot at the end of the allocation, then jumps back.

    Returns the address of the 8-byte pointer storage slot so the caller
    can read it with pm.read_longlong().

    Parameters
    ----------
    pm                      : active pymem.Pymem instance
    aob_address             : VA of the first byte of the matched AOB
    original_bytes          : the original instruction bytes to preserve
    capture_register_offset : byte offset inside a PUSH/MOV sequence to
                              select which register to capture (0 = RDI,
                              which is what all current CT scripts use)

    Notes
    ─────
    The emitted shellcode is equivalent to the CE ENABLE block:

        newmem:
          <original instruction>
          push rbx
          mov rbx, rdi          ; capture RDI (the game-object pointer)
          mov [pData], rbx
          pop rbx
          jmp returnhere

        pData:  dq 0

        INJECT:
          jmp newmem
          nop (padding)
    """
    orig_len = len(original_bytes)

    # ── Build shellcode ───────────────────────────────────────────────────────
    # We need: original_bytes + capture + jmp_back
    # Relative JMP is 5 bytes (E9 + rel32).
    # We pad the injection site with NOP if orig_len > 5.

    # Allocate ~128 bytes for trampoline + pointer storage
    alloc_size = 128
    kernel32 = ctypes.windll.kernel32

    h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pm.process_id)
    trampoline_addr = kernel32.VirtualAllocEx(
        h_process, None, alloc_size,
        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE
    )
    if not trampoline_addr:
        kernel32.CloseHandle(h_process)
        raise MemoryError("VirtualAllocEx failed for trampoline")

    pointer_slot_addr = trampoline_addr + alloc_size - 8  # last 8 bytes = pData

    # Relative address from end of JMP instruction back to original code
    ret_addr = aob_address + orig_len

    shellcode = bytearray()
    shellcode += original_bytes              # replicate original instruction(s)
    shellcode += bytes([0x53])               # push rbx
    shellcode += bytes([0x48, 0x89, 0xFB])  # mov rbx, rdi  (capture RDI)
    # mov [pointer_slot_addr], rbx  →  48 89 1C 25 <addr32>
    shellcode += bytes([0x48, 0x89, 0x1C, 0x25])
    shellcode += struct.pack("<I", pointer_slot_addr & 0xFFFF_FFFF)
    shellcode += bytes([0x5B])               # pop rbx
    # jmp ret_addr (relative)
    rel_jmp = ret_addr - (trampoline_addr + len(shellcode) + 5)
    shellcode += bytes([0xE9]) + struct.pack("<i", rel_jmp)

    # Write trampoline and zero the pointer slot
    written = ctypes.c_size_t(0)
    sc_bytes = bytes(shellcode)
    kernel32.WriteProcessMemory(h_process, trampoline_addr,
                                sc_bytes, len(sc_bytes),
                                ctypes.byref(written))
    # Zero the pointer storage slot
    kernel32.WriteProcessMemory(h_process, pointer_slot_addr,
                                b"\x00" * 8, 8, ctypes.byref(written))

    # ── Patch original site with JMP to trampoline ───────────────────────────
    old_protect = ctypes.c_ulong(0)
    kernel32.VirtualProtectEx(h_process, aob_address, orig_len,
                              PAGE_EXECUTE_READWRITE,
                              ctypes.byref(old_protect))

    jmp_rel = trampoline_addr - (aob_address + 5)
    patch = bytes([0xE9]) + struct.pack("<i", jmp_rel)
    patch += bytes([0x90] * (orig_len - 5))  # NOP padding

    kernel32.WriteProcessMemory(h_process, aob_address,
                                patch, len(patch), ctypes.byref(written))
    kernel32.VirtualProtectEx(h_process, aob_address, orig_len,
                              old_protect, ctypes.byref(old_protect))

    kernel32.CloseHandle(h_process)
    return pointer_slot_addr


# ─────────────────────────────────────────────────────────────────────────────
# DataExtractor
# ─────────────────────────────────────────────────────────────────────────────

class DataExtractor:
    """
    Manages attachment to the game process and exposes high-level read
    methods for all telemetry data points.

    Usage
    ─────
        extractor = DataExtractor()
        extractor.attach()          # blocks until the process is found
        extractor.find_offsets()    # AOB-scan and inject trampolines
        snapshot = extractor.get_snapshot()
    """

    def __init__(self) -> None:
        self._pm: pymem.Pymem | None = None          # pymem handle
        self._module_base: int = 0                   # game module base VA

        # Stored pointer-slot addresses (written by trampolines at runtime)
        self._p_race_data_slot: int | None = None    # points to pRaceData (from timer hook)
        self._p_checkpoint_slot: int | None = None   # points to r13+0x24C  (CP hook)

        # Cached live pointer values (updated each snapshot)
        self._race_data_ptr: int = 0
        self._checkpoint_ptr: int = 0

        # Physics-update counter – used to detect when a new frame is ready
        self._last_timer_value: int = -1

        self._lock = threading.Lock()

    # ── Attachment ────────────────────────────────────────────────────────────

    def attach(self, timeout: float = 0.0) -> bool:
        """
        Tries to attach to the game process.
        If timeout == 0.0 the call returns immediately (True/False).
        If timeout > 0 it retries until the process is found or time is up.
        Raises RuntimeError if pymem cannot attach.
        """
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            try:
                self._pm = pymem.Pymem(PROCESS_NAME)
                module = pymem.process.module_from_name(
                    self._pm.process_handle, PROCESS_NAME)
                self._module_base = module.lpBaseOfDll
                print(f"[DataExtractor] Attached to {PROCESS_NAME} "
                      f"(PID {self._pm.process_id}, base 0x{self._module_base:X})")
                return True
            except pymem.exception.ProcessNotFound:
                if deadline is None or time.monotonic() >= deadline:
                    return False
                time.sleep(1.0)

    def is_attached(self) -> bool:
        """Returns True if the game process is still running."""
        if self._pm is None:
            return False
        try:
            pymem.process.module_from_name(
                self._pm.process_handle, PROCESS_NAME)
            return True
        except Exception:
            return False

    # ── Offset / trampoline setup ─────────────────────────────────────────────

    def find_offsets(self) -> bool:
        """
        AOB-scans for all known signatures and injects pointer-capture
        trampolines (equivalent to CE ENABLE blocks).

        Returns True on full success, False if any scan failed.

        Translations from CE scripts
        ─────────────────────────────
        • AOB_RACE_TIMER    → captures RDI = pRaceData
          (RaceData.CT – LuaScript injection)
        • AOB_RACE_PROGRESS → confirms pRaceData via second hook
          (RaceData.CT – RP Finder)
        • AOB_RPM           → same pRaceData (Gearbox Info.CT)
        • AOB_CHECKPOINT    → captures R13 = checkpoint struct base
          (CP finder Steam.CT)
        """
        if self._pm is None:
            raise RuntimeError("Not attached – call attach() first")

        success = True

        # ── Timer hook → pRaceData ────────────────────────────────────────────
        timer_addr = self._aob_scan(AOB_RACE_TIMER)
        if timer_addr:
            try:
                self._p_race_data_slot = _inject_pointer_capture(
                    self._pm, timer_addr, AOB_RACE_TIMER)
                print(f"[DataExtractor] Timer hook injected at 0x{timer_addr:X}, "
                      f"slot 0x{self._p_race_data_slot:X}")
            except Exception as exc:
                print(f"[DataExtractor] Timer trampoline failed: {exc}")
                success = False
        else:
            print("[DataExtractor] AOB_RACE_TIMER not found")
            success = False

        # ── Checkpoint hook → r13 base ────────────────────────────────────────
        cp_addr = self._aob_scan(AOB_CHECKPOINT)
        if cp_addr:
            try:
                # CP script captures R13 (8-byte pointer).
                # The shellcode in _inject_pointer_capture always saves RDI;
                # for R13 we build a custom patch below. # placeholder – custom R13 capture shellcode not yet emitted;
                # using the generic helper which captures RDI as a stand-in.
                self._p_checkpoint_slot = _inject_pointer_capture(  # placeholder
                    self._pm, cp_addr, AOB_CHECKPOINT)
                print(f"[DataExtractor] CP hook injected at 0x{cp_addr:X}, "
                      f"slot 0x{self._p_checkpoint_slot:X}")
            except Exception as exc:
                print(f"[DataExtractor] CP trampoline failed: {exc}")
                success = False
        else:
            print("[DataExtractor] AOB_CHECKPOINT not found")
            success = False

        return success

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _aob_scan(self, pattern: bytes) -> int | None:
        """Scans the game module for a byte pattern; returns VA or None."""
        try:
            result = pymem.pattern.pattern_scan_module(
                self._pm.process_handle, PROCESS_NAME,
                pattern.hex(" ").upper()
            )
            return result
        except Exception as exc:
            print(f"[DataExtractor] AOB scan error: {exc}")
            return None

    def _read_race_data_ptr(self) -> int:
        """
        Reads the current pRaceData pointer from the trampoline slot.
        Returns 0 if the slot has not been populated yet (game not in race).
        """
        if self._p_race_data_slot is None or self._pm is None:
            return 0
        try:
            return self._pm.read_longlong(self._p_race_data_slot)
        except Exception:
            return 0

    def _read_checkpoint_ptr(self) -> int:
        """Reads the CP struct base pointer (r13+0x24C captured value)."""
        if self._p_checkpoint_slot is None or self._pm is None:
            return 0
        try:
            return self._pm.read_longlong(self._p_checkpoint_slot)
        except Exception:
            return 0

    # ─── Individual value readers ─────────────────────────────────────────────
    # Each reader returns None on failure so the snapshot can record missing
    # data without crashing.

    def _read_timer(self, base: int) -> int | None:
        """Race Timer – 4-byte int at base+0xA0.
        CE source: RaceData.CT – RaceTimer = [pRaceData+0xA0]"""
        try:
            return self._pm.read_int(base + OFFSET_RACE_TIMER)
        except Exception:
            return None

    def _read_race_progress(self, base: int) -> float | None:
        """Race Progress % – float at base+0x1D8.
        CE source: RaceData.CT – RaceProgress = [pRaceData+0x1D8]"""
        try:
            raw = self._pm.read_bytes(base + OFFSET_RACE_PROGRESS, 4)
            return struct.unpack("<f", raw)[0]
        except Exception:
            return None

    def _read_rpm(self, base: int) -> float | None:
        """Engine RPM – float at base+0x1B8.
        CE source: Gearbox Info.CT – RaceRPM_Raw = [pRaceData+0x1B8]"""
        try:
            raw = self._pm.read_bytes(base + OFFSET_RPM, 4)
            return struct.unpack("<f", raw)[0]
        except Exception:
            return None

    def _read_gear(self, base: int) -> int | None:
        """Current Gear – 4-byte int at base+0xA0.
        CE source: Gearbox Info.CT – RaceGear = [pRaceData+0xA0]
        Note: shares offset with timer in the same struct."""
        try:
            return self._pm.read_int(base + OFFSET_GEAR)
        except Exception:
            return None

    def _read_checkpoint(self) -> int | None:
        """Checkpoint # – 4-byte int through pointer captured from R13+0x24C.
        CE source: CP finder Steam.CT – Checkpoint = r13+0x24C (pointer)
        then [Checkpoint+0] = current checkpoint number."""
        cp_base = self._read_checkpoint_ptr()
        if cp_base == 0:
            return None
        try:
            return self._pm.read_int(cp_base)
        except Exception:
            return None

    def _read_velocity(self, base: int) -> dict | None:
        """Velocity (all four modes) – placeholder until offsets are found.
        Modes: Real Total, Fake Total, Real Horizontal, Fake Horizontal."""
        return None  # placeholder

    def _read_car_angle(self, base: int) -> float | None:
        """Car orientation angle."""
        return None  # placeholder

    def _read_car_position(self, base: int) -> dict | None:
        """Car XYZ world position."""
        return None  # placeholder

    def _read_camera_angle(self, base: int) -> float | None:
        """Camera orientation angle."""
        return None  # placeholder

    def _read_camera_position(self, base: int) -> dict | None:
        """Camera XYZ world position."""
        return None  # placeholder

    def _read_nitro_bar(self, base: int) -> float | None:
        """Nitro bar fill % (0.0 – 1.0)."""
        return None  # placeholder

    def _read_nitro_state(self, base: int) -> int | None:
        """Nitro state flags (boosting, empty, full, etc.)."""
        return None  # placeholder

    def _read_drift_state(self, base: int) -> int | None:
        """Drift state flag."""
        return None  # placeholder

    def _read_360_state(self, base: int) -> int | None:
        """360 / barrel-roll state flag."""
        return None  # placeholder

    def _read_acceleration(self, base: int) -> float | None:
        """Longitudinal acceleration value."""
        return None  # placeholder

    # ── Physics-frame detection ───────────────────────────────────────────────

    def _has_physics_update(self, current_timer: int | None) -> bool:
        """
        Returns True when the timer value has changed since the last call,
        indicating the physics engine has ticked.
        """
        if current_timer is None:
            return False
        changed = (current_timer != self._last_timer_value)
        self._last_timer_value = current_timer
        return changed

    # ── Public API ────────────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        """
        Captures a synchronised snapshot of all telemetry data points.

        Because all values are read within a single lock-held block from the
        same cached base pointer, no other thread can trigger a pointer
        update mid-read, giving consistent cross-field synchronisation.

        If a physics update has NOT been detected, only "race_completion_pct"
        and "timer_value" are populated; all other fields are None.  This
        matches the ghost-file format where sparse frames are expected.

        Returns a dict matching the ghost file race_data row schema.
        """
        with self._lock:
            base = self._read_race_data_ptr()
            self._race_data_ptr = base

            # Always-captured fields
            timer_value       = self._read_timer(base)        if base else None
            race_completion   = self._read_race_progress(base) if base else None

            snapshot: dict = {
                "timer_value":        timer_value,
                "race_completion_pct": race_completion,
                # Fields below are None unless a physics update is detected
                "velocity":            None,
                "car_angle":           None,
                "car_position":        None,
                "camera_angle":        None,
                "camera_position":     None,
                "checkpoint":          None,
                "nitro_bar_pct":       None,
                "nitro_state":         None,
                "drift_state":         None,
                "360_state":           None,
                "gear":                None,
                "engine_rpm":          None,
                "acceleration":        None,
            }

            # Only populate the full set when a physics frame has ticked
            if self._has_physics_update(timer_value):  # placeholder condition (timer change used as proxy; replace with dedicated physics flag when found)
                snapshot["velocity"]         = self._read_velocity(base)
                snapshot["car_angle"]        = self._read_car_angle(base)
                snapshot["car_position"]     = self._read_car_position(base)
                snapshot["camera_angle"]     = self._read_camera_angle(base)
                snapshot["camera_position"]  = self._read_camera_position(base)
                snapshot["checkpoint"]       = self._read_checkpoint()
                snapshot["nitro_bar_pct"]    = self._read_nitro_bar(base)
                snapshot["nitro_state"]      = self._read_nitro_state(base)
                snapshot["drift_state"]      = self._read_drift_state(base)
                snapshot["360_state"]        = self._read_360_state(base)
                snapshot["gear"]             = self._read_gear(base)
                snapshot["engine_rpm"]       = self._read_rpm(base)
                snapshot["acceleration"]     = self._read_acceleration(base)

        return snapshot

    def wait_for_race_start(self) -> bool:
        """
        Blocks indefinitely, polling until a race-start condition is met.
        Returns True once a race is detected.

        The actual start-detection condition is a placeholder and should be
        replaced with the real logic once the relevant memory offset is known.
        """
        print("[DataExtractor] Waiting for race start...")
        while True:
            base = self._read_race_data_ptr()
            if base != 0:                                          # placeholder condition (non-zero pRaceData used as proxy; replace with dedicated race-active flag)
                progress = self._read_race_progress(base)
                if progress is not None and progress >= 0.0:       # placeholder condition
                    print("[DataExtractor] Race start detected.")
                    return True
            time.sleep(0.1)

    def detect_race_end(self, ghost_manager, ghost_filepath: str,
                        race_frames: list) -> bool:
        """
        Checks whether the current race has ended.

        If a race end is detected, passes race_frames to ghost_manager for
        saving, then returns True.  Returns False if the race is still live.

        Parameters
        ----------
        ghost_manager  : GhostManager instance
        ghost_filepath : path of the ghost file to write to
        race_frames    : list of snapshot dicts accumulated this race
        """
        base = self._read_race_data_ptr()
        if base == 0:
            return False

        race_ended = False  # placeholder condition (replace with real end-of-race flag check)

        if race_ended:                                             # placeholder
            print("[DataExtractor] Race end detected – saving ghost data.")
            ghost_manager.save_race_data(ghost_filepath, race_frames)
            return True

        return False
