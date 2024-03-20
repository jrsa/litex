#
# This file is part of LiteX.
#
# Copyright (c) 2020-2022 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2020-2022 Dolu1990 <charles.papon.90@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

import os
import hashlib
import subprocess

from migen import *

from litex.gen import *

from litex import get_data_mod
from litex.soc.cores.cpu.naxriscv import NaxRiscv

from litex.soc.interconnect import axi
from litex.soc.interconnect.csr import *
from litex.soc.integration.soc import SoCRegion

from litex.soc.cores.cpu import CPU, CPU_GCC_TRIPLE_RISCV32, CPU_GCC_TRIPLE_RISCV64

# Variants -----------------------------------------------------------------------------------------

CPU_VARIANTS = {
    "standard": "VexiiRiscv",
}

# VexiiRiscv -----------------------------------------------------------------------------------------

class VexiiRiscv(CPU):
    category             = "softcore"
    family               = "riscv"
    name                 = "vexiiriscv"
    human_name           = "VexiiRiscv"
    variants             = CPU_VARIANTS
    data_width           = 32
    endianness           = "little"
    gcc_triple           = CPU_GCC_TRIPLE_RISCV32
    linker_output_format = "elf32-littleriscv"
    nop                  = "nop"
    io_regions           = {0x8000_0000: 0x8000_0000} # Origin, Length.

    # Default parameters.
    netlist_name     = None
    xlen             = 32
    internal_bus_width   = 32
    litedram_width   = 32
    l2_bytes         = 0
    l2_ways          = 8
    with_fpu         = False
    with_rvc         = False
    with_dma         = False
    jtag_tap         = False
    jtag_instruction = False

    # ABI.
    @staticmethod
    def get_abi():
        abi = "lp64" if VexiiRiscv.xlen == 64 else "ilp32"
        if VexiiRiscv.with_fpu:
            abi +="d"
        return abi

    # Arch.
    @staticmethod
    def get_arch():
        arch = f"rv{VexiiRiscv.xlen}i2p0_ma"
        if VexiiRiscv.with_fpu:
            arch += "fd"
        if VexiiRiscv.with_rvc:
            arch += "c"
        return arch

    # Memory Mapping.
    @property
    def mem_map(self):
        return {
            "rom":      0x0000_0000,
            "sram":     0x1000_0000,
            "main_ram": 0x4000_0000,
            "csr":      0xf000_0000,
            "clint":    0xf001_0000,
            "plic":     0xf0c0_0000,
        }

    # GCC Flags.
    @property
    def gcc_flags(self):
        flags =  f" -march={VexiiRiscv.get_arch()} -mabi={VexiiRiscv.get_abi()}"
        flags += f" -D__VexiiRiscv__"
        flags += f" -DUART_POLLING"
        return flags

    # Reserved Interrupts.
    @property
    def reserved_interrupts(self):
        return {"noirq": 0}

    # Command line configuration arguments.
    @staticmethod
    def args_fill(parser):
        cpu_group = parser.add_argument_group(title="CPU options")
        cpu_group.add_argument("--xlen",                  default=32,            help="Specify the RISC-V data width.")
        cpu_group.add_argument("--cpu-count",             default=1,             help="How many VexiiRiscv CPU.")
        cpu_group.add_argument("--with-coherent-dma",     action="store_true",   help="Enable coherent DMA accesses.")
        cpu_group.add_argument("--with-jtag-tap",         action="store_true",   help="Add a embedded JTAG tap for debugging.")
        cpu_group.add_argument("--with-jtag-instruction", action="store_true",   help="Add a JTAG instruction port which implement tunneling for debugging (TAP not included).")
        cpu_group.add_argument("--update-repo",           default="recommended", choices=["latest","wipe+latest","recommended","wipe+recommended","no"], help="Specify how the VexiiRiscv & SpinalHDL repo should be updated (latest: update to HEAD, recommended: Update to known compatible version, no: Don't update, wipe+*: Do clean&reset before checkout)")
        cpu_group.add_argument("--no-netlist-cache",      action="store_true",   help="Always (re-)build the netlist.")
        cpu_group.add_argument("--with-fpu",              action="store_true",   help="Enable the F32/F64 FPU.")
        cpu_group.add_argument("--with-rvc",              action="store_true",   help="Enable the Compress ISA extension.")
        cpu_group.add_argument("--l2-bytes",              default=128*1024,      help="VexiiRiscv L2 bytes, default 128 KB.")
        cpu_group.add_argument("--l2-ways",               default=8,             help="VexiiRiscv L2 ways, default 8.")

    @staticmethod
    def args_read(args):
        print(args)

        VexiiRiscv.jtag_tap         = args.with_jtag_tap
        VexiiRiscv.jtag_instruction = args.with_jtag_instruction
        VexiiRiscv.with_dma         = args.with_coherent_dma
        VexiiRiscv.update_repo      = args.update_repo
        VexiiRiscv.no_netlist_cache = args.no_netlist_cache
        VexiiRiscv.with_fpu         = args.with_fpu
        VexiiRiscv.with_rvc         = args.with_rvc
        if args.xlen:
            xlen = int(args.xlen)
            VexiiRiscv.internal_bus_width   = xlen
            VexiiRiscv.xlen                 = xlen
            VexiiRiscv.data_width           = xlen
            VexiiRiscv.gcc_triple           = CPU_GCC_TRIPLE_RISCV64
            VexiiRiscv.linker_output_format = f"elf{xlen}-littleriscv"
        if args.cpu_count:
            VexiiRiscv.cpu_count = args.cpu_count
        if args.l2_bytes:
            VexiiRiscv.l2_bytes = args.l2_bytes
        if args.l2_ways:
            VexiiRiscv.l2_ways = args.l2_ways


    def __init__(self, platform, variant):
        self.platform         = platform
        self.variant          = "standard"
        self.reset            = Signal()
        self.interrupt        = Signal(32)
        self.pbus             = pbus = axi.AXILiteInterface(address_width=32, data_width=32)

        self.periph_buses     = [pbus] # Peripheral buses (Connected to main SoC's bus).
        self.memory_buses     = []           # Memory buses (Connected directly to LiteDRAM).

        # # #

        self.tracer_valid = Signal()
        self.tracer_payload = Signal(8)

        # CPU Instance.
        self.cpu_params = dict(
            # Clk/Rst.
            i_socClk     = ClockSignal("sys"),
            i_asyncReset = ResetSignal("sys") | self.reset,

            # Patcher/Tracer.
            # o_patcher_tracer_valid   = self.tracer_valid,
            # o_patcher_tracer_payload = self.tracer_payload,

            # Interrupt.
            i_peripheral_externalInterrupts_port = self.interrupt,

            # Peripheral Memory Bus (AXI Lite Slave).
            o_pBus_awvalid = pbus.aw.valid,
            i_pBus_awready = pbus.aw.ready,
            o_pBus_awaddr  = pbus.aw.addr,
            o_pBus_awprot  = Open(),
            o_pBus_wvalid  = pbus.w.valid,
            i_pBus_wready  = pbus.w.ready,
            o_pBus_wdata   = pbus.w.data,
            o_pBus_wstrb   = pbus.w.strb,
            i_pBus_bvalid  = pbus.b.valid,
            o_pBus_bready  = pbus.b.ready,
            i_pBus_bresp   = pbus.b.resp,
            o_pBus_arvalid = pbus.ar.valid,
            i_pBus_arready = pbus.ar.ready,
            o_pBus_araddr  = pbus.ar.addr,
            o_pBus_arprot  = Open(),
            i_pBus_rvalid  = pbus.r.valid,
            o_pBus_rready  = pbus.r.ready,
            i_pBus_rdata   = pbus.r.data,
            i_pBus_rresp   = pbus.r.resp,
        )

        if VexiiRiscv.with_dma:
            self.dma_bus = dma_bus = axi.AXIInterface(data_width=VexiiRiscv.perf_bus_width, address_width=32, id_width=4)

            self.cpu_params.update(
                # DMA Bus.
                # --------
                # AW Channel.
                o_dma_bus_awready = dma_bus.aw.ready,
                i_dma_bus_awvalid = dma_bus.aw.valid,
                i_dma_bus_awid    = dma_bus.aw.id,
                i_dma_bus_awaddr  = dma_bus.aw.addr,
                i_dma_bus_awlen   = dma_bus.aw.len,
                i_dma_bus_awsize  = dma_bus.aw.size,
                i_dma_bus_awburst = dma_bus.aw.burst,
                i_dma_bus_awlock  = dma_bus.aw.lock,
                i_dma_bus_awcache = dma_bus.aw.cache,
                i_dma_bus_awprot  = dma_bus.aw.prot,
                i_dma_bus_awqos   = dma_bus.aw.qos,

                # W Channel.
                o_dma_bus_wready  = dma_bus.w.ready,
                i_dma_bus_wvalid  = dma_bus.w.valid,
                i_dma_bus_wdata   = dma_bus.w.data,
                i_dma_bus_wstrb   = dma_bus.w.strb,
                i_dma_bus_wlast   = dma_bus.w.last,

                # B Channel.
                i_dma_bus_bready  = dma_bus.b.ready,
                o_dma_bus_bvalid  = dma_bus.b.valid,
                o_dma_bus_bid     = dma_bus.b.id,
                o_dma_bus_bresp   = dma_bus.b.resp,

                # AR Channel.
                o_dma_bus_arready = dma_bus.ar.ready,
                i_dma_bus_arvalid = dma_bus.ar.valid,
                i_dma_bus_arid    = dma_bus.ar.id,
                i_dma_bus_araddr  = dma_bus.ar.addr,
                i_dma_bus_arlen   = dma_bus.ar.len,
                i_dma_bus_arsize  = dma_bus.ar.size,
                i_dma_bus_arburst = dma_bus.ar.burst,
                i_dma_bus_arlock  = dma_bus.ar.lock,
                i_dma_bus_arcache = dma_bus.ar.cache,
                i_dma_bus_arprot  = dma_bus.ar.prot,
                i_dma_bus_arqos   = dma_bus.ar.qos,

                # R Channel.
                i_dma_bus_rready  = dma_bus.r.ready,
                o_dma_bus_rvalid  = dma_bus.r.valid,
                o_dma_bus_rid     = dma_bus.r.id,
                o_dma_bus_rdata   = dma_bus.r.data,
                o_dma_bus_rresp   = dma_bus.r.resp,
                o_dma_bus_rlast   = dma_bus.r.last,
            )

    def set_reset_address(self, reset_address):
        self.reset_address = reset_address

    # Cluster Name Generation.
    @staticmethod
    def generate_netlist_name(reset_address):
        md5_hash = hashlib.md5()
        md5_hash.update(str(reset_address).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.litedram_width).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.xlen).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.cpu_count).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.l2_bytes).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.l2_ways).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.jtag_tap).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.jtag_instruction).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.with_dma).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.memory_regions).encode('utf-8'))
        md5_hash.update(str(VexiiRiscv.internal_bus_width).encode('utf-8'))


        digest = md5_hash.hexdigest()
        VexiiRiscv.netlist_name = "VexiiRiscvLitex_" + digest

    # Netlist Generation.
    @staticmethod
    def generate_netlist(reset_address):
        vdir = get_data_mod("cpu", "vexiiriscv").data_location
        ndir = os.path.join(vdir, "ext", "VexiiRiscv")
        sdir = os.path.join(vdir, "ext", "SpinalHDL")

        #if VexiiRiscv.update_repo != "no":
        #    NaxRiscv.git_setup("VexiiRiscv", ndir, "https://github.com/SpinalHDL/VexiiRiscv.git", "main", "ec3ee4dc" if VexiiRiscv.update_repo=="recommended" else None)

        gen_args = []
        gen_args.append(f"--netlist-name={VexiiRiscv.netlist_name}")
        gen_args.append(f"--netlist-directory={vdir}")
        gen_args.append(f"--reset-vector={reset_address}")
        gen_args.append(f"--xlen={VexiiRiscv.xlen}")
        gen_args.append(f"--cpu-count={VexiiRiscv.cpu_count}")
        gen_args.append(f"--l2-bytes={VexiiRiscv.l2_bytes}")
        gen_args.append(f"--l2-ways={VexiiRiscv.l2_ways}")
        gen_args.append(f"--litedram-width={VexiiRiscv.litedram_width}")
        gen_args.append(f"--internal_bus_width={VexiiRiscv.internal_bus_width}")
        for region in VexiiRiscv.memory_regions:
            gen_args.append(f"--memory-region={region[0]},{region[1]},{region[2]},{region[3]}")
        for args in VexiiRiscv.scala_args:
            gen_args.append(f"--scala-args={args}")
        if(VexiiRiscv.jtag_tap) :
            gen_args.append(f"--with-jtag-tap")
        if(VexiiRiscv.jtag_instruction) :
            gen_args.append(f"--with-jtag-instruction")
        if(VexiiRiscv.jtag_tap or VexiiRiscv.jtag_instruction):
            gen_args.append(f"--with-debug")
        if(VexiiRiscv.with_dma) :
            gen_args.append(f"--with-dma")
        for file in VexiiRiscv.scala_paths:
            gen_args.append(f"--scala-file={file}")
        if(VexiiRiscv.with_fpu):
            gen_args.append(f"--scala-args=rvf=true,rvd=true")
        if(VexiiRiscv.with_rvc):
            gen_args.append(f"--scala-args=rvc=true")

        cmd = f"""cd {ndir} && sbt "runMain vexiiriscv.platform.litex.VexiiGen {" ".join(gen_args)}\""""
        print("VexiiRiscv generation command :")
        print(cmd)
        subprocess.check_call(cmd, shell=True)


    def add_sources(self, platform):
        vdir = get_data_mod("cpu", "vexiiriscv").data_location
        print(f"VexiiRiscv netlist : {self.netlist_name}")

        if VexiiRiscv.no_netlist_cache or not os.path.exists(os.path.join(vdir, self.netlist_name + ".v")):
            self.generate_netlist(self.reset_address)

        # Add RAM.
        # By default, use Generic RAM implementation.
        ram_filename = "Ram_1w_1rs_Generic.v"
        # On Altera/Intel platforms, use specific implementation.
        from litex.build.altera import AlteraPlatform
        if isinstance(platform, AlteraPlatform):
            ram_filename = "Ram_1w_1rs_Intel.v"
        # On Efinix platforms, use specific implementation.
        from litex.build.efinix import EfinixPlatform
        if isinstance(platform, EfinixPlatform):
            ram_filename = "Ram_1w_1rs_Efinix.v"
        platform.add_source(os.path.join(vdir, ram_filename), "verilog")

        # Add Cluster.
        platform.add_source(os.path.join(vdir,  self.netlist_name + ".v"), "verilog")

    def add_soc_components(self, soc):
        # Set Human-name.
        self.human_name = f"{self.human_name} {self.xlen}-bit"

        # Set UART/Timer0 CSRs to the ones used by OpenSBI.
        soc.csr.add("uart",   n=2)
        soc.csr.add("timer0", n=3)

        # Add OpenSBI region.
        soc.bus.add_region("opensbi", SoCRegion(origin=self.mem_map["main_ram"] + 0x00f0_0000, size=0x8_0000, cached=True, linker=True))

        # Define ISA.
        soc.add_config("CPU_COUNT", VexiiRiscv.cpu_count)
        soc.add_config("CPU_ISA", VexiiRiscv.get_arch())
        soc.add_config("CPU_MMU", {32 : "sv32", 64 : "sv39"}[VexiiRiscv.xlen])

        soc.bus.add_region("plic",  SoCRegion(origin=soc.mem_map.get("plic"),  size=0x40_0000, cached=False,  linker=True))
        soc.bus.add_region("clint", SoCRegion(origin=soc.mem_map.get("clint"), size= 0x1_0000, cached=False,  linker=True))

        if VexiiRiscv.jtag_tap:
            self.jtag_tms = Signal()
            self.jtag_tck = Signal()
            self.jtag_tdi = Signal()
            self.jtag_tdo = Signal()

            self.cpu_params.update(
                i_jtag_tms = self.jtag_tms,
                i_jtag_tck = self.jtag_tck,
                i_jtag_tdi = self.jtag_tdi,
                o_jtag_tdo = self.jtag_tdo,
            )

        if VexiiRiscv.jtag_instruction:
            self.jtag_clk     = Signal()
            self.jtag_enable  = Signal()
            self.jtag_capture = Signal()
            self.jtag_shift   = Signal()
            self.jtag_update  = Signal()
            self.jtag_reset   = Signal()
            self.jtag_tdo     = Signal()
            self.jtag_tdi     = Signal()

            self.cpu_params.update(
                i_jtag_instruction_clk     = self.jtag_clk,
                i_jtag_instruction_enable  = self.jtag_enable,
                i_jtag_instruction_capture = self.jtag_capture,
                i_jtag_instruction_shift   = self.jtag_shift,
                i_jtag_instruction_update  = self.jtag_update,
                i_jtag_instruction_reset   = self.jtag_reset,
                i_jtag_instruction_tdi     = self.jtag_tdi,
                o_jtag_instruction_tdo     = self.jtag_tdo,
            )

        if VexiiRiscv.jtag_instruction or VexiiRiscv.jtag_tap:
            # Create PoR Clk Domain for debug_reset.
            self.cd_debug_por = ClockDomain()
            self.comb += self.cd_debug_por.clk.eq(ClockSignal("sys"))

            # Create PoR debug_reset.
            debug_reset = Signal(reset=1)
            self.sync.debug_por += debug_reset.eq(0)

            # Debug resets.
            debug_ndmreset      = Signal()
            debug_ndmreset_last = Signal()
            debug_ndmreset_rise = Signal()
            self.cpu_params.update(
                i_debug_reset    = debug_reset,
                o_debug_ndmreset = debug_ndmreset,
            )

            # Reset SoC's CRG when debug_ndmreset rising edge.
            self.sync.debug_por += debug_ndmreset_last.eq(debug_ndmreset)
            self.comb += debug_ndmreset_rise.eq(debug_ndmreset & ~debug_ndmreset_last)
            self.comb += If(debug_ndmreset_rise, soc.crg.rst.eq(1))

        self.soc_bus = soc.bus # FIXME: Save SoC Bus instance to retrieve the final mem layout on finalization.

    def add_memory_buses(self, address_width, data_width):
        VexiiRiscv.litedram_width = data_width

        mbus = axi.AXIInterface(
            data_width    = VexiiRiscv.litedram_width,
            address_width = 32,
            id_width      = 8, #TODO
        )
        self.memory_buses.append(mbus)

        self.cpu_params.update(
            # Memory Bus (Master).
            # --------------------
            # AW Channel.
            o_mBus_awvalid   = mbus.aw.valid,
            i_mBus_awready   = mbus.aw.ready,
            o_mBus_awaddr    = mbus.aw.addr,
            o_mBus_awid      = mbus.aw.id,
            o_mBus_awlen     = mbus.aw.len,
            o_mBus_awsize    = mbus.aw.size,
            o_mBus_awburst   = mbus.aw.burst,
            o_mBus_awallStrb = Open(),
            # W Channel.
            o_mBus_wvalid    = mbus.w.valid,
            i_mBus_wready    = mbus.w.ready,
            o_mBus_wdata     = mbus.w.data,
            o_mBus_wstrb     = mbus.w.strb,
            o_mBus_wlast     = mbus.w.last,
            # B Channel.
            i_mBus_bvalid    = mbus.b.valid,
            o_mBus_bready    = mbus.b.ready,
            i_mBus_bid       = mbus.b.id,
            i_mBus_bresp     = mbus.b.resp,
            # AR Channel.
            o_mBus_arvalid   = mbus.ar.valid,
            i_mBus_arready   = mbus.ar.ready,
            o_mBus_araddr    = mbus.ar.addr,
            o_mBus_arid      = mbus.ar.id,
            o_mBus_arlen     = mbus.ar.len,
            o_mBus_arsize    = mbus.ar.size,
            o_mBus_arburst   = mbus.ar.burst,
            # R Channel.
            i_mBus_rvalid    = mbus.r.valid,
            o_mBus_rready    = mbus.r.ready,
            i_mBus_rdata     = mbus.r.data,
            i_mBus_rid       = mbus.r.id,
            i_mBus_rresp     = mbus.r.resp,
            i_mBus_rlast     = mbus.r.last,
        )

    def do_finalize(self):
        assert hasattr(self, "reset_address")

        # Generate memory map from CPU perspective
        # vexiiriscv modes:
        # r,w,x,c  : readable, writeable, executable, caching allowed
        # io       : IO region (Implies P bus, preserve memory order, no dcache)
        # vexiiriscv bus:
        # p        : peripheral
        # m        : memory
        VexiiRiscv.memory_regions = []
        for name, region in self.soc_bus.io_regions.items():
            VexiiRiscv.memory_regions.append( (region.origin, region.size, "io", "p") ) # IO is only allowed on the p bus
        for name, region in self.soc_bus.regions.items():
            if region.linker: # Remove virtual regions.
                continue
            if len(self.memory_buses) and name == 'main_ram': # m bus
                bus = "m"
            else:
                bus = "p"
            mode = region.mode
            mode += "c" if region.cached else ""
            VexiiRiscv.memory_regions.append( (region.origin, region.size, mode, bus) )

        self.generate_netlist_name(self.reset_address)

        # Do verilog instance.
        self.specials += Instance(self.netlist_name, **self.cpu_params)

        # Add verilog sources.
        self.add_sources(self.platform)
