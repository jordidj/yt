import numpy as np

from yt.data_objects.static_output import ParticleFile
from yt.frontends.sph.data_structures import SPHDataset, SPHParticleIndex
from yt.funcs import only_on_root
from yt.utilities.logger import ytLogger as mylog
from yt.utilities.on_demand_imports import _h5py as h5py

from .fields import SwiftFieldInfo


class SwiftParticleFile(ParticleFile):
    pass


class SwiftDataset(SPHDataset):
    _load_requirements = ["h5py"]
    _index_class = SPHParticleIndex
    _field_info_class = SwiftFieldInfo
    _file_class = SwiftParticleFile

    _particle_mass_name = "Masses"
    _particle_coordinates_name = "Coordinates"
    _particle_velocity_name = "Velocities"
    _sph_ptypes = ("PartType0",)
    _suffix = ".hdf5"

    def __init__(
        self,
        filename,
        dataset_type="swift",
        storage_filename=None,
        units_override=None,
        unit_system="cgs",
        default_species_fields=None,
    ):
        super().__init__(
            filename,
            dataset_type,
            units_override=units_override,
            unit_system=unit_system,
            default_species_fields=default_species_fields,
        )
        self.storage_filename = storage_filename
        self.refine_by = 1

    def _set_code_unit_attributes(self):
        """
        Sets the units from the SWIFT internal unit system.

        Currently sets length, mass, time, and temperature.

        SWIFT uses comoving coordinates without the usual h-factors.
        """
        units = self._get_info_attributes("Units")

        if self.cosmological_simulation == 1:
            msg = "Assuming length units are in comoving centimetres"
            only_on_root(mylog.info, msg)
            self.length_unit = self.quan(
                float(units["Unit length in cgs (U_L)"]), "cmcm"
            )
        else:
            msg = "Assuming length units are in physical centimetres"
            only_on_root(mylog.info, msg)
            self.length_unit = self.quan(float(units["Unit length in cgs (U_L)"]), "cm")

        self.mass_unit = self.quan(float(units["Unit mass in cgs (U_M)"]), "g")
        self.time_unit = self.quan(float(units["Unit time in cgs (U_t)"]), "s")
        self.temperature_unit = self.quan(
            float(units["Unit temperature in cgs (U_T)"]), "K"
        )

        return

    def _get_info_attributes(self, dataset):
        """
        Gets the information from a header-style dataset and returns it as a
        python dictionary.

        Example: self._get_info_attributes(header) returns a dictionary of all
        of the information in the Header.attrs.
        """

        with h5py.File(self.filename, mode="r") as handle:
            header = dict(handle[dataset].attrs)

        return header

    def _parse_parameter_file(self):
        """
        Parse the SWIFT "parameter file" -- really this actually reads info
        from the main HDF5 file as everything is replicated there and usually
        parameterfiles are not transported.

        The header information from the HDF5 file is stored in an un-parsed
        format in self.parameters should users wish to use it.
        """
        # Read from the HDF5 file, this gives us all the info we need. The rest
        # of this function is just parsing.
        header = self._get_info_attributes("Header")
        # RuntimePars were removed from snapshots at SWIFT commit 6271388
        # between SWIFT versions 0.8.5 and 0.9.0
        with h5py.File(self.filename, mode="r") as handle:
            has_runtime_pars = "RuntimePars" in handle.keys()

        if has_runtime_pars:
            runtime_parameters = self._get_info_attributes("RuntimePars")
        else:
            runtime_parameters = {}

        policy = self._get_info_attributes("Policy")
        # These are the parameterfile parameters from *.yml at runtime
        parameters = self._get_info_attributes("Parameters")

        # Not used in this function, but passed to parameters
        hydro = self._get_info_attributes("HydroScheme")
        subgrid = self._get_info_attributes("SubgridScheme")

        self.domain_right_edge = header["BoxSize"]
        self.domain_left_edge = np.zeros_like(self.domain_right_edge)

        self.dimensionality = int(header["Dimension"])

        # SWIFT is either all periodic, or not periodic at all
        if has_runtime_pars:
            periodic = int(runtime_parameters["PeriodicBoundariesOn"])
        else:
            periodic = int(parameters["InitialConditions:periodic"])

        if periodic:
            self._periodicity = [True] * self.dimensionality
        else:
            self._periodicity = [False] * self.dimensionality

        # Units get attached to this
        self.current_time = float(header["Time"])

        # Now cosmology enters the fray, as a runtime parameter.
        self.cosmological_simulation = int(policy["cosmological integration"])

        if self.cosmological_simulation:
            try:
                self.current_redshift = float(header["Redshift"])
                # These won't be present if self.cosmological_simulation is false
                self.omega_lambda = float(parameters["Cosmology:Omega_lambda"])
                # Cosmology:Omega_m parameter deprecated at SWIFT commit d2783c2
                # Between SWIFT versions 0.9.0 and 1.0.0
                if "Cosmology:Omega_cdm" in parameters:
                    self.omega_matter = float(parameters["Cosmology:Omega_b"]) + float(
                        parameters["Cosmology:Omega_cdm"]
                    )
                else:
                    self.omega_matter = float(parameters["Cosmology:Omega_m"])
                # This is "little h"
                self.hubble_constant = float(parameters["Cosmology:h"])
            except KeyError:
                mylog.warning(
                    "Could not find cosmology information in Parameters, "
                    "despite having ran with -c signifying a cosmological "
                    "run."
                )
                mylog.info("Setting up as a non-cosmological run. Check this!")
                self.cosmological_simulation = 0
                self.current_redshift = 0.0
                self.omega_lambda = 0.0
                self.omega_matter = 0.0
                self.hubble_constant = 0.0
        else:
            self.current_redshift = 0.0
            self.omega_lambda = 0.0
            self.omega_matter = 0.0
            self.hubble_constant = 0.0

        # Store the un-parsed information should people want it.
        self.parameters = {
            "header": header,
            "policy": policy,
            "parameters": parameters,
            # NOTE: runtime_parameters may be empty
            "runtime_parameters": runtime_parameters,
            "hydro": hydro,
            "subgrid": subgrid,
        }

        # SWIFT never has multi file snapshots
        self.file_count = 1
        self.filename_template = self.parameter_filename

        return

    @classmethod
    def _is_valid(cls, filename: str, *args, **kwargs) -> bool:
        """
        Checks to see if the file is a valid output from SWIFT.
        This requires the file to have the Code attribute set in the
        Header dataset to "SWIFT".
        """
        if cls._missing_load_requirements():
            return False

        valid = True
        # Attempt to open the file, if it's not a hdf5 then this will fail:
        try:
            handle = h5py.File(filename, mode="r")
            valid = handle["Header"].attrs["Code"].decode("utf-8") == "SWIFT"
            handle.close()
        except (OSError, KeyError):
            valid = False

        return valid
