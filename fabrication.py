"""This module handles the generation of the Gerber files, the BOM and the POS file."""

import csv
import logging
import os
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from pcbnew import (  # pylint: disable=import-error
    EXCELLON_WRITER,
    PCB_PLOT_PARAMS,
    PLOT_CONTROLLER,
    PLOT_FORMAT_GERBER,
    ZONE_FILLER,
    B_Cu,
    B_Mask,
    B_Paste,
    B_SilkS,
    Cmts_User,
    Edge_Cuts,
    F_Cu,
    F_Mask,
    F_Paste,
    F_SilkS,
    GetBoard,
    Refresh,
    ToMM,
)

# Compatibility hack for V6 / V7 / V7.99
try:
    from pcbnew import DRILL_MARKS_NO_DRILL_SHAPE  # pylint: disable=import-error

    NO_DRILL_SHAPE = DRILL_MARKS_NO_DRILL_SHAPE
except ImportError:
    NO_DRILL_SHAPE = PCB_PLOT_PARAMS.NO_DRILL_SHAPE

from .helpers import get_exclude_from_pos, get_footprint_by_ref, get_smd


class Fabrication:
    """This class contains all functionality to generate the JLCPCB production files."""

    def __init__(self, parent):
        self.parent = parent
        self.logger = logging.getLogger(__name__)
        self.board = GetBoard()
        self.corrections = []
        self.path, self.filename = os.path.split(self.board.GetFileName())
        self.create_folders()

    def create_folders(self):
        """Create output folders if they not already exist."""
        self.outputdir = os.path.join(self.path, "jlcpcb", "production_files")
        Path(self.outputdir).mkdir(parents=True, exist_ok=True)
        self.gerberdir = os.path.join(self.path, "jlcpcb", "gerber")
        Path(self.gerberdir).mkdir(parents=True, exist_ok=True)

    def fill_zones(self):
        """Refill copper zones following user prompt."""
        if self.parent.settings.get("gerber", {}).get("fill_zones", True):
            filler = ZONE_FILLER(self.board)
            zones = self.board.Zones()
            filler.Fill(zones)
            Refresh()

    def fix_rotation(self, footprint):
        """Fix the rotation of footprints in order to be correct for JLCPCB."""
        original = footprint.GetOrientation()
        # `.AsDegrees()` added in KiCAD 6.99
        try:
            rotation = original.AsDegrees()
        except AttributeError:
            # we need to divide by 10 to get 180 out of 1800 for example.
            # This might be a bug in 5.99 / 6.0 RC
            rotation = original / 10
        if footprint.GetLayer() != 0:
            # bottom angles need to be mirrored on Y-axis
            rotation = (180 - rotation) % 360
        # First check if the value aka part name matches
        for regex, correction in self.corrections:
            if re.search(regex, str(footprint.GetValue())):
                return self.rotate(footprint, rotation, correction)
        # Then if the package matches
        for regex, correction in self.corrections:
            if re.search(regex, str(footprint.GetFPID().GetLibItemName())):
                return self.rotate(footprint, rotation, correction)
        # If no correction matches, return the original rotation
        return rotation

    def rotate(self, footprint, rotation, correction):
        """Calculate the actual correction"""
        if footprint.GetLayer() == 0:
            rotation = (rotation + int(correction)) % 360
            self.logger.info(
                "Fixed rotation of %s (%s / %s) on Top Layer by %d degrees",
                footprint.GetReference(),
                footprint.GetValue(),
                footprint.GetFPID().GetLibItemName(),
                correction,
            )
        else:
            rotation = (rotation - int(correction)) % 360
            self.logger.info(
                "Fixed rotation of %s (%s / %s) on Bottom Layer by %d degrees",
                footprint.GetReference(),
                footprint.GetValue(),
                footprint.GetFPID().GetLibItemName(),
                correction,
            )
        return rotation

    def get_position(self, footprint):
        """Calculate position based on center of bounding box"""
        if get_smd(footprint):
            return footprint.GetPosition()
        bbox = footprint.GetBoundingBox(False, False)
        return bbox.GetCenter()

    def generate_geber(self, layer_count=None):
        """Generating Gerber files"""
        # inspired by https://github.com/KiCad/kicad-source-mirror/blob/master/demos/python_scripts_examples/gen_gerber_and_drill_files_board.py
        pctl = PLOT_CONTROLLER(self.board)
        popt = pctl.GetPlotOptions()

        # https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_plot_params.h
        popt.SetOutputDirectory(self.gerberdir)

        # Plot format to Gerber
        # https://github.com/KiCad/kicad-source-mirror/blob/master/include/plotter.h#L67-L78
        popt.SetFormat(1)

        # General Options
        popt.SetPlotValue(
            self.parent.settings.get("gerber", {}).get("plot_values", True)
        )
        popt.SetPlotReference(
            self.parent.settings.get("gerber", {}).get("plot_references", True)
        )
        popt.SetPlotInvisibleText(False)

        popt.SetSketchPadsOnFabLayers(False)

        # Gerber Options
        popt.SetUseGerberProtelExtensions(False)

        popt.SetCreateGerberJobFile(False)

        popt.SetSubtractMaskFromSilk(True)

        popt.SetPlotViaOnMaskLayer(False)  # Set this to True if you need untented vias

        popt.SetUseAuxOrigin(True)

        # Tented vias or not, selcted by user in settings
        popt.SetPlotViaOnMaskLayer(
            not self.parent.settings.get("gerber", {}).get("tented_vias", True)
        )

        popt.SetUseGerberX2format(True)

        popt.SetIncludeGerberNetlistInfo(True)

        popt.SetDisableGerberMacros(False)

        popt.SetDrillMarksType(NO_DRILL_SHAPE)

        popt.SetPlotFrameRef(False)

        # delete all existing files in the output directory first
        for f in os.listdir(self.gerberdir):
            os.remove(os.path.join(self.gerberdir, f))

        # if no layer_count is given, get the layer count from the board
        if not layer_count:
            layer_count = self.board.GetCopperLayerCount()

        plot_plan_top = [
            ("CuTop", F_Cu, "Top layer"),
            ("SilkTop", F_SilkS, "Silk top"),
            ("MaskTop", F_Mask, "Mask top"),
            ("PasteTop", F_Paste, "Paste top"),
        ]
        plot_plan_bottom = [
            ("CuBottom", B_Cu, "Bottom layer"),
            ("SilkBottom", B_SilkS, "Silk top"),
            ("MaskBottom", B_Mask, "Mask bottom"),
            ("PasteBottom", B_Paste, "Paste bottom"),
            ("EdgeCuts", Edge_Cuts, "Edges"),
            ("VScore", Cmts_User, "V score cut"),
        ]

        plot_plan = []

        # Single sided PCB
        if layer_count == 1:
            plot_plan = plot_plan_top + plot_plan_bottom[-2:]
        # Double sided PCB
        elif layer_count == 2:
            plot_plan = plot_plan_top + plot_plan_bottom
        # Everything with inner layers
        else:
            plot_plan = (
                plot_plan_top
                + [
                    (f"CuIn{layer}", layer, f"Inner layer {layer}")
                    for layer in range(1, layer_count - 1)
                ]
                + plot_plan_bottom
            )

        for layer_info in plot_plan:
            if layer_info[1] <= B_Cu:
                popt.SetSkipPlotNPTH_Pads(True)
            else:
                popt.SetSkipPlotNPTH_Pads(False)
            pctl.SetLayer(layer_info[1])
            pctl.OpenPlotfile(layer_info[0], PLOT_FORMAT_GERBER, layer_info[2])
            if pctl.PlotLayer() is False:
                self.logger.error("Error plotting %s", layer_info[2])
            self.logger.info("Successfully plotted %s", layer_info[2])
        pctl.ClosePlot()

    def generate_excellon(self):
        """Generate Excellon files."""
        drlwriter = EXCELLON_WRITER(self.board)
        mirror = False
        minimalHeader = False
        offset = self.board.GetDesignSettings().GetAuxOrigin()
        mergeNPTH = False
        drlwriter.SetOptions(mirror, minimalHeader, offset, mergeNPTH)
        drlwriter.SetFormat(False)
        genDrl = True
        genMap = True
        drlwriter.CreateDrillandMapFilesSet(self.gerberdir, genDrl, genMap)
        self.logger.info("Finished generating Excellon files")

    def zip_gerber_excellon(self):
        """Zip Gerber and Excellon files, ready for upload to JLCPCB."""
        zipname = f"GERBER-{self.filename.split('.')[0]}.zip"
        with ZipFile(
            os.path.join(self.outputdir, zipname),
            "w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
        ) as zipfile:
            for folderName, _, filenames in os.walk(self.gerberdir):
                for filename in filenames:
                    if not filename.endswith(("gbr", "drl", "pdf")):
                        continue
                    filePath = os.path.join(folderName, filename)
                    zipfile.write(filePath, os.path.basename(filePath))
        self.logger.info("Finished generating ZIP file")

    def generate_cpl(self):
        """Generate placement file (CPL)."""
        cplname = f"CPL-{self.filename.split('.')[0]}.csv"
        self.corrections = self.parent.library.get_all_correction_data()
        aux_orgin = self.board.GetDesignSettings().GetAuxOrigin()
        add_without_lcsc = self.parent.settings.get("gerber", {}).get(
            "lcsc_bom_pos", True
        )
        with open(
            os.path.join(self.outputdir, cplname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(
                ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]
            )
            for part in self.parent.store.read_pos_parts():
                for fp in get_footprint_by_ref(self.board, part[0]):
                    if get_exclude_from_pos(fp):
                        continue
                    self.logger.debug(
                        "'%s' '%s' '%s'",
                        add_without_lcsc,
                        part[4],
                        (not add_without_lcsc and not part[4]),
                    )
                    if not add_without_lcsc and not part[4]:
                        continue
                    self.logger.debug(part)
                    position = self.get_position(fp) - aux_orgin
                    writer.writerow(
                        [
                            part[0],
                            part[1],
                            part[2],
                            ToMM(position.x),
                            ToMM(position.y) * -1,
                            self.fix_rotation(fp),
                            "top" if fp.GetLayer() == 0 else "bottom",
                        ]
                    )
        self.logger.info("Finished generating CPL file")

    def generate_bom(self):
        """Generate BOM file."""
        bomname = f"BOM-{self.filename.split('.')[0]}.csv"
        with open(
            os.path.join(self.outputdir, bomname), "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.writer(csvfile, delimiter=",")
            writer.writerow(["Comment", "Designator", "Footprint", "LCSC"])
            for part in self.parent.store.read_bom_parts():
                writer.writerow(part)
        self.logger.info("Finished generating BOM file")
