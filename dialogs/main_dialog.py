"""
Main dialog for Plasticity Bridge plugin.

UI uses Cinema 4D's native QUICKTAB interface:
  Three tabs — Server, Basic, Utilities — with QUICKTAB_BAR section headers
  before each tab's content for visual separation when multiple tabs are open.
  Shift-click opens multiple tabs simultaneously, exactly like native C4D.
  All label+input pairs are in unified 2-column grids for proper alignment.
"""

import c4d
import math
from c4d import gui
from core.threading_bridge import EventType, BridgeEvent
from core.protocol import FacetShapeType, MessageType
from core.refacet_tag import (
    TAG_PLUGIN_ID as REFACET_TAG_ID,
    PREF_TOPOLOGY, PREF_TOPOLOGY_TRIS, PREF_TOPOLOGY_NGONS,
    PREF_OPTIONS_MODE, PREF_OPTIONS_SIMPLE, PREF_OPTIONS_ADVANCED,
    PREF_TOLERANCE, PREF_ANGLE,
    PREF_MIN_WIDTH, PREF_MAX_WIDTH,
    PREF_CURVE_CHORD_TOL, PREF_CURVE_CHORD_ANG,
    PREF_SURF_PLANE_TOL, PREF_SURF_ANGLE_TOL,
)

PLUGIN_ID = 1066929

# Tab indices for the QuickTab
TAB_SERVER    = 0
TAB_BASIC     = 1
TAB_UTILITIES = 2


class IDS:
    # Top-level
    GRP_MAIN             = 1000

    # QuickTab bar
    QUICKTAB             = 1010

    # Tab content groups (shown / hidden by quicktab selection)
    GRP_TAB_SERVER       = 1020
    GRP_TAB_BASIC        = 1021
    GRP_TAB_UTILITIES    = 1022

    # Section headers (QUICKTAB_BAR)
    HDR_SERVER           = 1030
    HDR_BASIC            = 1031
    HDR_UTILITIES        = 1032

    # Server tab
    EDT_SERVER           = 1101
    BTN_CONNECT          = 1102
    BTN_DISCONNECT       = 1103
    LBL_STATUS           = 1200
    LBL_FILENAME         = 1201
    CHK_ONLY_VISIBLE     = 1300
    BTN_REFRESH          = 1301
    CHK_LIVELINK         = 1400
    SLD_UNIT_SCALE       = 1800

    # Basic tab (refacet)
    TOGGLE_TOPOLOGY      = 1500
    TOGGLE_REFACET_OPTS  = 1501
    GRP_SIMPLE_OPTIONS   = 1502
    GRP_ADVANCED_OPTIONS = 1503
    SLD_TOLERANCE        = 1504
    SLD_ANGLE            = 1505
    SLD_MIN_WIDTH        = 1600
    SLD_MAX_WIDTH        = 1601
    SLD_CURVE_CHORD_TOL  = 1602
    SLD_CURVE_CHORD_ANG  = 1603
    SLD_SURF_PLANE_TOL   = 1604
    SLD_SURF_ANGLE_TOL   = 1605
    BTN_REFACET          = 1700
    CHK_AUTO_REFACET     = 1701

    # Utilities tab
    BTN_STORE_FACES      = 1901
    BTN_STORE_EDGES      = 1902
    BTN_SELECT_SHARP     = 1903
    EDT_SHARP_ANGLE      = 1905
    BTN_SELECT_FACE      = 1906
    BTN_SELECT_EDGE      = 1907


def _add_section_header(dlg, gadget_id, title):
    """Add a QUICKTAB_BAR section header — the thin titled separator bar."""
    bc = c4d.BaseContainer()
    bc.SetBool(c4d.QUICKTAB_BAR, True)
    bc.SetString(c4d.QUICKTAB_BARTITLE, title)
    dlg.AddCustomGui(gadget_id, c4d.CUSTOMGUI_QUICKTAB, "",
                     c4d.BFH_SCALEFIT, 0, 0, bc)


class PlasticityDialog(gui.GeDialog):
    TIMER_INTERVAL = 16   # ~60 fps

    def __init__(self, client, handler, bridge):
        super().__init__()
        self.client   = client
        self.handler  = handler
        self.bridge   = bridge

        # Dialog state
        self._server            = "localhost:8980"
        self._only_visible      = False
        self._tri_mode          = True
        self._advanced_mode     = False
        self._tolerance         = 0.01
        self._angle             = math.radians(25.0)
        self._min_width         = 0.0
        self._max_width         = 0.0
        self._curve_chord_tol   = 0.01
        self._curve_chord_angle = math.radians(20.0)
        self._surface_plane_tol = 0.01
        self._surface_angle_tol = math.radians(20.0)
        self._unit_scale        = 1.0
        self._busy              = False

        # QuickTab widget references
        self._quicktab          = None
        self._toggle_topology   = None
        self._toggle_refacet    = None

        # Register bridge callbacks
        bridge.register_callback(EventType.CONNECTED,        self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,     self._on_disconnected)
        bridge.register_callback(EventType.CONNECTION_ERROR, self._on_connection_error)
        bridge.register_callback(EventType.NEW_VERSION,      self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,         self._on_new_file)
        bridge.register_callback(EventType.LIST_RESPONSE,    self._on_operation_complete)
        bridge.register_callback(EventType.REFACET_RESPONSE, self._on_operation_complete)
        bridge.register_callback(EventType.STATUS_UPDATE,    self._on_status_update)
        # v2.1.0
        bridge.register_callback(EventType.PUT_SOME_RESPONSE, self._on_operation_complete)

    # =========================================================================
    # Layout
    # =========================================================================

    # Fixed width for all left-column labels
    LABEL_W = 160

    def CreateLayout(self):
        self.SetTitle("Plasticity Bridge")
        LW = self.LABEL_W

        if self.GroupBegin(IDS.GRP_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                           cols=1, rows=0):
            self.GroupBorderSpace(4, 4, 4, 4)

            # ── QuickTab bar ─────────────────────────────────────────────
            bc = c4d.BaseContainer()
            bc.SetBool(c4d.QUICKTAB_BAR, False)
            bc.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
            bc.SetBool(c4d.QUICKTAB_NOMULTISELECT, False)

            self._quicktab = self.AddCustomGui(
                IDS.QUICKTAB, c4d.CUSTOMGUI_QUICKTAB, "",
                c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc)

            if self._quicktab:
                self._quicktab.AppendString(TAB_SERVER,    "Server",    True)
                self._quicktab.AppendString(TAB_BASIC,     "Basic",     True)
                self._quicktab.AppendString(TAB_UTILITIES, "Utilities", True)

            # ── Tab: Server ──────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_SERVER,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_SERVER, "Server")

                # Server address
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 4, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Server")
                    self.AddEditText(IDS.EDT_SERVER, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                # Connect / Disconnect
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW, name="")
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                        self.AddButton(IDS.BTN_CONNECT, c4d.BFH_SCALEFIT,
                                       name="Connect")
                        self.AddButton(IDS.BTN_DISCONNECT, c4d.BFH_SCALEFIT,
                                       name="Disconnect")
                    self.GroupEnd()
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Status
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0):
                    self.GroupBorderSpace(4, 2, 4, 2)
                    self.AddStaticText(IDS.LBL_STATUS, c4d.BFH_SCALEFIT,
                                       name="Status: Disconnected")
                    self.AddStaticText(IDS.LBL_FILENAME, c4d.BFH_SCALEFIT,
                                       name="File: -")
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Only Visible + Refresh
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 0)
                    self.AddCheckbox(IDS.CHK_ONLY_VISIBLE, c4d.BFH_LEFT,
                                     initw=LW, inith=0, name="Only Visible")
                    self.AddButton(IDS.BTN_REFRESH, c4d.BFH_SCALEFIT,
                                   name="Refresh")
                self.GroupEnd()

                # Live Link
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0):
                    self.GroupBorderSpace(4, 0, 4, 2)
                    self.AddCheckbox(IDS.CHK_LIVELINK, c4d.BFH_LEFT,
                                     initw=0, inith=0, name="Live Link")
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Unit Scale
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Unit Scale")
                    self.AddEditSlider(IDS.SLD_UNIT_SCALE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_SERVER

            # ── Tab: Basic (refacet) ─────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_BASIC,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_BASIC, "Basic")

                # Topology toggle
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 4, 4, 2)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Topology")
                    bc_topo = c4d.BaseContainer()
                    bc_topo.SetBool(c4d.QUICKTAB_BAR, False)
                    bc_topo.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
                    bc_topo.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
                    self._toggle_topology = self.AddCustomGui(
                        IDS.TOGGLE_TOPOLOGY, c4d.CUSTOMGUI_QUICKTAB, "",
                        c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc_topo)
                    if self._toggle_topology:
                        self._toggle_topology.AppendString(0, "Tris",  True)
                        self._toggle_topology.AppendString(1, "Ngons", False)
                self.GroupEnd()

                # Refacet Options toggle
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Refacet Options")
                    bc_opts = c4d.BaseContainer()
                    bc_opts.SetBool(c4d.QUICKTAB_BAR, False)
                    bc_opts.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
                    bc_opts.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
                    self._toggle_refacet = self.AddCustomGui(
                        IDS.TOGGLE_REFACET_OPTS, c4d.CUSTOMGUI_QUICKTAB,
                        "", c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc_opts)
                    if self._toggle_refacet:
                        self._toggle_refacet.AppendString(0, "Simple",   True)
                        self._toggle_refacet.AppendString(1, "Advanced", False)
                self.GroupEnd()

                # Simple options group (Tolerance + Angle)
                if self.GroupBegin(IDS.GRP_SIMPLE_OPTIONS,
                                   c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 2)
                    self.GroupBorderSpace(4, 2, 4, 0)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Tolerance")
                    self.AddEditSlider(IDS.SLD_TOLERANCE, c4d.BFH_SCALEFIT)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Angle")
                    self.AddEditSlider(IDS.SLD_ANGLE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                # Advanced options group
                if self.GroupBegin(IDS.GRP_ADVANCED_OPTIONS,
                                   c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 2)
                    self.GroupBorderSpace(4, 2, 4, 0)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Min Width")
                    self.AddEditSlider(IDS.SLD_MIN_WIDTH, c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Max Width")
                    self.AddEditSlider(IDS.SLD_MAX_WIDTH, c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Edge Chord Tol")
                    self.AddEditSlider(IDS.SLD_CURVE_CHORD_TOL,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Edge Chord Angle")
                    self.AddEditSlider(IDS.SLD_CURVE_CHORD_ANG,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Face Plane Tol")
                    self.AddEditSlider(IDS.SLD_SURF_PLANE_TOL,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Face Angle Tol")
                    self.AddEditSlider(IDS.SLD_SURF_ANGLE_TOL,
                                       c4d.BFH_SCALEFIT)
                self.GroupEnd()  # GRP_ADVANCED_OPTIONS

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Refacet Selected
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW, name="")
                    self.AddButton(IDS.BTN_REFACET, c4d.BFH_SCALEFIT,
                                   name="Refacet Selected")
                self.GroupEnd()

                # Auto-Refacet
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW, name="")
                    self.AddCheckbox(IDS.CHK_AUTO_REFACET, c4d.BFH_LEFT,
                                     initw=0, inith=0, name="Auto-Refacet")
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_BASIC

            # ── Tab: Utilities ───────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_UTILITIES,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_UTILITIES, "Utilities")

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 4, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Store")
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                        self.AddButton(IDS.BTN_STORE_FACES, c4d.BFH_SCALEFIT,
                                       name="All Plasticity Faces")
                        self.AddButton(IDS.BTN_STORE_EDGES, c4d.BFH_SCALEFIT,
                                       name="All Plasticity Edges")
                    self.GroupEnd()
                self.GroupEnd()

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Select")
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                        self.AddButton(IDS.BTN_SELECT_FACE, c4d.BFH_SCALEFIT,
                                       name="Plasticity Face(s)")
                        self.AddButton(IDS.BTN_SELECT_EDGE, c4d.BFH_SCALEFIT,
                                       name="Plasticity Edges")
                    self.GroupEnd()
                self.GroupEnd()

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Sharp")
                    self.AddButton(IDS.BTN_SELECT_SHARP,
                                   c4d.BFH_SCALEFIT,
                                   name="Select Sharp Edges")
                self.GroupEnd()

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="")
                    if self.GroupBegin(0, c4d.BFH_LEFT, 2, 0):
                        self.GroupSpace(4, 0)
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Angle")
                        self.AddEditNumberArrows(IDS.EDT_SHARP_ANGLE,
                                           c4d.BFH_LEFT, initw=80)
                    self.GroupEnd()
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_UTILITIES

        self.GroupEnd()  # GRP_MAIN
        return True

    # =========================================================================
    # Init values
    # =========================================================================

    def InitValues(self):
        self.SetString(IDS.EDT_SERVER, self._server)
        self.SetBool(IDS.CHK_ONLY_VISIBLE, self._only_visible)

        # Simple sliders
        self.SetFloat(IDS.SLD_TOLERANCE, self._tolerance,
                      min=0.0001, max=0.1, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_ANGLE, self._angle,
                      min=math.radians(1.0), max=math.radians(90.0),
                      step=math.radians(1.0),
                      format=c4d.FORMAT_DEGREE)

        # Advanced sliders
        self.SetFloat(IDS.SLD_MIN_WIDTH, self._min_width,
                      min=0.0, max=10.0, step=0.01,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_MAX_WIDTH, self._max_width,
                      min=0.0, max=1000.0, step=0.1,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_TOL, self._curve_chord_tol,
                      min=0.0001, max=1.0, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_ANG, self._curve_chord_angle,
                      min=math.radians(1.0), max=math.radians(90.0),
                      step=math.radians(1.0),
                      format=c4d.FORMAT_DEGREE)
        self.SetFloat(IDS.SLD_SURF_PLANE_TOL, self._surface_plane_tol,
                      min=0.0001, max=1.0, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_SURF_ANGLE_TOL, self._surface_angle_tol,
                      min=math.radians(1.0), max=math.radians(90.0),
                      step=math.radians(1.0),
                      format=c4d.FORMAT_DEGREE)

        # Unit scale
        self.SetFloat(IDS.SLD_UNIT_SCALE, self._unit_scale,
                      min=0.0001, max=100.0, step=0.01,
                      format=c4d.FORMAT_FLOAT)

        # Auto-Refacet checkbox
        self.SetBool(IDS.CHK_AUTO_REFACET, False)

        # Sharp edge angle (degrees)
        self.SetFloat(IDS.EDT_SHARP_ANGLE, 0.0,
                      min=0.0, max=math.pi, step=math.radians(1.0),
                      format=c4d.FORMAT_DEGREE)

        # Refacet options: show Simple group, hide Advanced group by default
        self._sync_refacet_options()

        # Apply initial quicktab visibility
        self._sync_tab_visibility()

        self._update_ui_state()
        self.SetTimer(self.TIMER_INTERVAL)
        return True

    # =========================================================================
    # QuickTab visibility sync
    # =========================================================================

    def _sync_tab_visibility(self):
        if not self._quicktab:
            return

        show_server    = self._quicktab.IsSelected(TAB_SERVER)
        show_basic     = self._quicktab.IsSelected(TAB_BASIC)
        show_utilities = self._quicktab.IsSelected(TAB_UTILITIES)

        self.HideElement(IDS.GRP_TAB_SERVER,    not show_server)
        self.HideElement(IDS.GRP_TAB_BASIC,     not show_basic)
        self.HideElement(IDS.GRP_TAB_UTILITIES, not show_utilities)

        self.LayoutChanged(IDS.GRP_MAIN)

    def _sync_refacet_options(self):
        if self._toggle_refacet:
            self._advanced_mode = self._toggle_refacet.IsSelected(1)
        self.HideElement(IDS.GRP_SIMPLE_OPTIONS,   self._advanced_mode)
        self.HideElement(IDS.GRP_ADVANCED_OPTIONS,  not self._advanced_mode)
        self.LayoutChanged(IDS.GRP_TAB_BASIC)

    # =========================================================================
    # Timer
    # =========================================================================

    def Timer(self, _):
        self.bridge.process_pending_events()
        self._update_ui_state()

    # =========================================================================
    # Commands
    # =========================================================================

    def Command(self, id, _):
        # ── QuickTab toggled ─────────────────────────────────────────────
        if id == IDS.QUICKTAB:
            self._sync_tab_visibility()
            return True

        # ── Connection ───────────────────────────────────────────────────
        if id == IDS.BTN_CONNECT:
            if self._busy:
                return True
            self._server = self.GetString(IDS.EDT_SERVER)
            self.client.connect(self._server)

        elif id == IDS.BTN_DISCONNECT:
            self.client.disconnect()

        # ── Refresh (v2.1.0: triggers PutSome after list completes) ──────
        elif id == IDS.BTN_REFRESH:
            if self._busy:
                return True
            self._busy = True
            self._only_visible = self.GetBool(IDS.CHK_ONLY_VISIBLE)

            # Capture local refs for the closure
            client  = self.client
            handler = self.handler
            only_visible = self._only_visible

            def on_complete(filename):
                """Called on the main thread after the list response is processed."""
                if not client.supports(MessageType.PUT_SOME_1):
                    return

                outbox_data = handler.get_outbox_data(filename, only_visible)
                if not outbox_data["groups"] and not outbox_data["items"]:
                    return

                # Use the C4D document filename, or fall back
                doc = c4d.documents.GetActiveDocument()
                c4d_filename = "Untitled.c4d"
                if doc:
                    name = doc.GetDocumentName()
                    if name:
                        c4d_filename = name

                client.put_some(c4d_filename,
                                outbox_data["groups"],
                                outbox_data["items"])

            if self._only_visible:
                self.client.list_visible(on_complete)
            else:
                self.client.list_all(on_complete)

        # ── Live Link ────────────────────────────────────────────────────
        elif id == IDS.CHK_LIVELINK:
            if self.GetBool(IDS.CHK_LIVELINK):
                self.client.subscribe_all()
            else:
                self.client.unsubscribe()

        # ── Unit Scale ───────────────────────────────────────────────────
        elif id == IDS.SLD_UNIT_SCALE:
            self._unit_scale = self.GetFloat(IDS.SLD_UNIT_SCALE)
            self.handler.update_unit_scale(self._unit_scale)

        # ── Topology toggle ───────────────────────────────────────────
        elif id == IDS.TOGGLE_TOPOLOGY:
            if self._toggle_topology:
                self._tri_mode = self._toggle_topology.IsSelected(0)

        # ── Refacet Options toggle ───────────────────────────────────
        elif id == IDS.TOGGLE_REFACET_OPTS:
            self._sync_refacet_options()

        # ── Refacet ──────────────────────────────────────────────────────
        elif id == IDS.BTN_REFACET:
            if self._busy:
                return True
            self._busy = True
            self._do_refacet()

        # ── Utilities ────────────────────────────────────────────────────
        elif id == IDS.BTN_STORE_FACES:
            self._do_store_faces()

        elif id == IDS.BTN_STORE_EDGES:
            self._do_store_edges()

        elif id == IDS.BTN_SELECT_FACE:
            self._do_select_plasticity_faces()

        elif id == IDS.BTN_SELECT_EDGE:
            self._do_select_plasticity_edges()

        elif id == IDS.BTN_SELECT_SHARP:
            self._do_select_sharp_edges()

        return True

    # =========================================================================
    # UI state
    # =========================================================================

    def _update_ui_state(self):
        connected = self.bridge.connected

        self.Enable(IDS.BTN_CONNECT,    not connected and not self._busy)
        self.Enable(IDS.BTN_DISCONNECT, connected)
        self.Enable(IDS.EDT_SERVER,     not connected)
        self.Enable(IDS.BTN_REFRESH,    connected and not self._busy)
        self.Enable(IDS.CHK_LIVELINK,   connected)
        self.Enable(IDS.SLD_UNIT_SCALE, connected)
        self.Enable(IDS.CHK_ONLY_VISIBLE, connected)
        self.Enable(IDS.BTN_REFACET,    connected and not self._busy)
        self.Enable(IDS.CHK_AUTO_REFACET, connected)
        self.Enable(IDS.TOGGLE_TOPOLOGY,     connected)
        self.Enable(IDS.TOGGLE_REFACET_OPTS, connected)
        self.Enable(IDS.SLD_TOLERANCE,       connected)
        self.Enable(IDS.SLD_ANGLE,           connected)
        self.Enable(IDS.SLD_MIN_WIDTH,       connected)
        self.Enable(IDS.SLD_MAX_WIDTH,       connected)
        self.Enable(IDS.SLD_CURVE_CHORD_TOL, connected)
        self.Enable(IDS.SLD_CURVE_CHORD_ANG, connected)
        self.Enable(IDS.SLD_SURF_PLANE_TOL,  connected)
        self.Enable(IDS.SLD_SURF_ANGLE_TOL,  connected)
        doc = c4d.documents.GetActiveDocument()
        in_poly_mode = bool(doc and doc.GetMode() == c4d.Mpolygons)
        self.Enable(IDS.BTN_STORE_FACES, connected and not self._busy)
        self.Enable(IDS.BTN_STORE_EDGES, connected and not self._busy)
        self.Enable(IDS.BTN_SELECT_FACE, connected and not self._busy and in_poly_mode)
        self.Enable(IDS.BTN_SELECT_EDGE, connected and not self._busy and in_poly_mode)
        self.Enable(IDS.BTN_SELECT_SHARP, connected and not self._busy)
        self.Enable(IDS.EDT_SHARP_ANGLE, connected)

        status = self.bridge.status_message
        self.SetString(IDS.LBL_STATUS,   f"Status: {status}")
        filename = self.bridge.filename or "-"
        self.SetString(IDS.LBL_FILENAME, f"File: {filename}")

    # =========================================================================
    # Bridge event callbacks
    # =========================================================================

    def _on_connected(self, _: BridgeEvent):
        self._busy = False

    def _on_disconnected(self, _: BridgeEvent):
        self._busy = False
        self.SetBool(IDS.CHK_LIVELINK, False)

    def _on_connection_error(self, event: BridgeEvent):
        self._busy = False
        error = event.error_message or "Unknown error"
        gui.MessageDialog(f"Connection error:\n{error}")

    def _on_operation_complete(self, _: BridgeEvent):
        self._busy = False

    def _on_status_update(self, _: BridgeEvent):
        self._busy = False

    def _on_new_version(self, _: BridgeEvent):
        if self.GetBool(IDS.CHK_LIVELINK) and not self._busy:
            self._busy = True
            if self.GetBool(IDS.CHK_ONLY_VISIBLE):
                self.client.list_visible()
            else:
                self.client.list_all()

    def _on_new_file(self, _: BridgeEvent):
        if self.GetBool(IDS.CHK_LIVELINK) and not self._busy:
            self._busy = True
            if self.GetBool(IDS.CHK_ONLY_VISIBLE):
                self.client.list_visible()
            else:
                self.client.list_all()

    # =========================================================================
    # Refacet
    # =========================================================================

    def _do_refacet(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            self._busy = False
            return

        ids = self.handler.get_selected_plasticity_ids(doc)
        if not ids:
            gui.MessageDialog(
                "No Plasticity objects selected.\n"
                "Select one or more Plasticity mesh objects first.")
            self._busy = False
            return

        by_filename = {}
        for filename, obj_id in ids:
            by_filename.setdefault(filename, []).append(obj_id)

        tolerance = self.GetFloat(IDS.SLD_TOLERANCE)
        angle     = self.GetFloat(IDS.SLD_ANGLE)
        min_width = self.GetFloat(IDS.SLD_MIN_WIDTH)
        max_width = self.GetFloat(IDS.SLD_MAX_WIDTH)

        max_sides       = 3   if self._tri_mode else 128
        plane_angle     = 0.0 if self._tri_mode else math.pi / 4.0
        curve_chord_max = max_width * math.sqrt(0.5)

        if self._advanced_mode:
            cct = self.GetFloat(IDS.SLD_CURVE_CHORD_TOL)
            cca = self.GetFloat(IDS.SLD_CURVE_CHORD_ANG)
            spt = self.GetFloat(IDS.SLD_SURF_PLANE_TOL)
            spa = self.GetFloat(IDS.SLD_SURF_ANGLE_TOL)
        else:
            cct = tolerance
            cca = angle
            spt = tolerance
            spa = angle

        for filename, obj_ids in by_filename.items():
            self.client.refacet_some(
                filename              = filename,
                plasticity_ids        = obj_ids,
                relative_to_bbox      = True,
                curve_chord_tolerance = cct,
                curve_chord_angle     = cca,
                surface_plane_tolerance = spt,
                surface_plane_angle   = spa,
                match_topology        = True,
                max_sides             = max_sides,
                plane_angle           = plane_angle,
                min_width             = min_width,
                max_width             = max_width,
                curve_chord_max       = curve_chord_max,
                shape                 = FacetShapeType.CUT,
            )

        # ── Auto-Refacet tag creation / update ───────────────────────────
        if self.GetBool(IDS.CHK_AUTO_REFACET):
            self._stamp_auto_refacet_tags(doc, ids, tolerance, angle,
                                          min_width, max_width, cct, cca,
                                          spt, spa)

    def _stamp_auto_refacet_tags(self, doc, ids, tolerance, angle,
                                 min_width, max_width, cct, cca, spt, spa):
        """Create or update auto-refacet tags on the selected Plasticity objects."""
        doc.StartUndo()
        try:
            for filename, obj_id in ids:
                # Find the C4D object via the handler's cache
                key = (filename, obj_id)
                obj = self.handler._items.get(key)
                if not obj or obj.GetDocument() != doc:
                    continue

                # Find existing auto-refacet tag or create a new one
                tag = obj.GetTag(REFACET_TAG_ID)
                if tag:
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
                else:
                    tag = c4d.BaseTag(REFACET_TAG_ID)
                    if not tag:
                        continue
                    obj.InsertTag(tag)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, tag)

                # Write current dialog settings into the tag
                tag[PREF_TOPOLOGY] = (PREF_TOPOLOGY_TRIS
                                      if self._tri_mode
                                      else PREF_TOPOLOGY_NGONS)
                tag[PREF_OPTIONS_MODE] = (PREF_OPTIONS_ADVANCED
                                          if self._advanced_mode
                                          else PREF_OPTIONS_SIMPLE)
                tag[PREF_TOLERANCE]       = tolerance
                tag[PREF_ANGLE]           = angle
                tag[PREF_MIN_WIDTH]       = min_width
                tag[PREF_MAX_WIDTH]       = max_width
                tag[PREF_CURVE_CHORD_TOL] = cct
                tag[PREF_CURVE_CHORD_ANG] = cca
                tag[PREF_SURF_PLANE_TOL]  = spt
                tag[PREF_SURF_ANGLE_TOL]  = spa
        finally:
            doc.EndUndo()

        c4d.EventAdd()

    # =========================================================================
    # Store Plasticity Faces
    # =========================================================================

    def _do_store_faces(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        affected = self.handler.apply_face_selection_tags(doc)
        if not affected:
            gui.MessageDialog(
                "No Plasticity objects selected.\n"
                "Select one or more Plasticity mesh objects first.")

    # =========================================================================
    # Store Plasticity Edges
    # =========================================================================

    def _do_store_edges(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        affected = self.handler.apply_edge_selection_tags(doc)
        if not affected:
            gui.MessageDialog(
                "No Plasticity objects selected.\n"
                "Select one or more Plasticity mesh objects first.")

    # =========================================================================
    # Select Plasticity Face(s) / Edges
    # =========================================================================

    def _do_select_plasticity_faces(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        affected = self.handler.select_plasticity_faces(doc)
        if not affected:
            gui.MessageDialog(
                "No Plasticity faces selected.\n"
                "Select one or more polygons on a Plasticity object first.")

    def _do_select_plasticity_edges(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        affected = self.handler.select_plasticity_edges(doc)
        if not affected:
            gui.MessageDialog(
                "No Plasticity faces selected.\n"
                "Select one or more polygons on a Plasticity object first.")

    # =========================================================================
    # Select Sharp Edges
    # =========================================================================

    def _do_select_sharp_edges(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        angle_deg = math.degrees(self.GetFloat(IDS.EDT_SHARP_ANGLE))
        affected = self.handler.select_sharp_edges(doc, angle_deg=angle_deg)
        if not affected:
            gui.MessageDialog(
                "No Plasticity objects selected.\n"
                "Select one or more Plasticity mesh objects first.")

    # =========================================================================
    # Layout restore and cleanup
    # =========================================================================

    def RestoreLayout(self, pluginid, secret):
        return self.Restore(pluginid=pluginid, secret=secret)

    def DestroyWindow(self):
        self.SetTimer(0)
        if self.bridge.connected:
            self.client.disconnect()
        return super().DestroyWindow()