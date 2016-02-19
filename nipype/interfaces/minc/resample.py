import os
import numpy as np

from base import MINCCommand, MINCCommandInputSpec, Info
from nipype.interfaces.base import (TraitedSpec, File, traits, InputMultiPath,isdefined)






class ResampleOutput(TraitedSpec):
    out_file = File(exists=True, desc="resampled image")

class ResampleInput(MINCCommandInputSpec):
    in_file = File(position=0, argstr="%s", mandatory=True, desc="image to resample")
    out_file = File(position=1, argstr="%s", desc="resampled image")
    model_file = File(position=2, argstr="-like %s", mandatory=True, desc="model image")
    
    transformation = File(argstr="-transformation %s", desc="image to resample")
    interpolation = traits.Enum('trilinear', 'tricubic', 'nearest_neighbour', 'sinc', argstr="-%s", desc="interpolation type", default='trilinear')
    
    clobber = traits.Bool(argstr="-clobber", usedefault=True, default_value=True, desc="Overwrite output file")
    verbose = traits.Bool(argstr="-verbose", usedefault=True, default_value=True, desc="Write messages indicating progress")

class ResampleCommand(MINCCommand):
    _cmd = "mincresample"
    _suffix = "_resample"
    input_spec = ResampleInput
    output_spec = ResampleOutput

    def _parse_inputs(self, skip=None):
        if skip is None:
            skip = []
        if not isdefined(self.inputs.out_file):
            self.inputs.out_file = self._gen_fname(self.inputs.in_file, suffix=self._suffix)

        return super(ResampleCommand, self)._parse_inputs(skip=skip)


    def _list_outputs(self):
        outputs = self.output_spec().get()
        outputs["out_file"] = self.inputs.out_file
        return outputs


    def _gen_filename(self, name):
        if name == "out_file":
            return self._list_outputs()["out_file"]
        return None


