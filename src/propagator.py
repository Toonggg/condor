# -----------------------------------------------------------------------------------------------------
# CONDOR
# Simulator for diffractive single-particle imaging experiments with X-ray lasers
# http://xfel.icm.uu.se/condor/
# -----------------------------------------------------------------------------------------------------
# Copyright 2014 Max Hantke, Filipe R.N.C. Maia, Tomas Ekeberg
# Condor is distributed under the terms of the GNU General Public License
# -----------------------------------------------------------------------------------------------------
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but without any warranty; without even the implied warranty of
# merchantability or fitness for a pariticular purpose. See the
# GNU General Public License for more details.
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
# -----------------------------------------------------------------------------------------------------
# General note:
# All variables are in SI units by default. Exceptions explicit by variable name.
# -----------------------------------------------------------------------------------------------------

import sys
import numpy

import tempfile, os
   
import logging
logger = logging.getLogger("Condor")
import utils.log
from utils.log import log 
from utils.pixelmask import PixelMask

import utils
import utils.sphere_diffraction
import utils.spheroid_diffraction
import utils.scattering_vector
import utils.resample
from particle_sphere import ParticleModelSphere
from particle_spheroid import ParticleModelSpheroid
from particle_map import ParticleModelMap
from particle_molecule import ParticleModelMolecule, get_spsim_conf

class Propagator:
    def __init__(self,source,sample,detector):
        self.source   = source
        self.sample   = sample
        self.detector = detector
        self._qmap_cache = {}

    def propagate(self,**kwargs):
        N = self.sample.number_of_images
        O = {"source":{},"sample":{},"detector":{}}
        O_particles = {}
        N_particles_max = 0

        for i in range(N):
            log(logger.info,"Calculation diffraction pattern (%i/%i). (PROGSTAT)" % (i+1,N))

            Os = self._propagate_single(**kwargs)

            N_particles = len(Os["sample"]["particles"])
            print "%i/%i (%i particle%s)" % (i+1,N,N_particles,"s" if N_particles > 1 else "")

            for k in [k for k in Os.keys() if k not in ["source","sample","detector"]]:
                if k not in O:
                    O[k] = [None for _ in range(N)]
                O[k][i] = Os[k]
            # Source
            for k,v in Os["source"].items():
                if k not in O["source"]:
                    O["source"][k] = [None for _ in range(N)]
                O["source"][k][i] = Os["source"][k]
            # Sample
            for k in [k for k in Os["sample"].keys() if k != "particles"]:
                if k not in O["sample"]:
                    O["sample"][k] = [None for _ in range(N)]
                O["sample"][k][i] = Os["sample"][k]
            for j,p in zip(range(len(Os["sample"]["particles"])),Os["sample"]["particles"]):
                for k,v in p.items():
                    if k not in O_particles:
                        O_particles[k] = [[] for _ in range(N)]
                    O_particles[k][i].append(v)
            N_particles_max = max([N_particles_max,N_particles])
            # Detector
            for k,v in Os["detector"].items():
                if k not in O["detector"]:
                    O["detector"][k] = [None] * N
                O["detector"][k][i] = Os["detector"][k]
        # Make arrays for particle parameters
        for k,v in O_particles.items():
            s = [N,N_particles_max]
            v0 = numpy.array(v[0])
            s_item = list(v0.shape)
            s_item.pop(0)
            s = s + s_item
            A = numpy.zeros(shape=s,dtype=v0.dtype)
            for i in range(len(v)):
                vi = numpy.array(v[i])
                d = len(vi.shape)
                if d == 1:
                    A[i,:vi.shape[0]] = vi[:]
                elif d == 2:
                    A[i,:vi.shape[0],:] = vi[:,:]
                elif d == 3:
                    A[i,:vi.shape[0],:,:] = vi[:,:,:]
                elif d == 4:
                    A[i,:vi.shape[0],:,:,:] = vi[:,:,:,:]
            O["sample"][k] = A 
        for k in O["source"].keys(): O["source"][k] = numpy.array(O["source"][k])
        for k in O["detector"].keys(): O["detector"][k] = numpy.array(O["detector"][k])
        for k in [k for k in O.keys() if k not in ["source","sample","detector"]]:
            O[k] = numpy.array(O[k])
        return O

    def _propagate_single(self,**kwargs):
        save_map = kwargs.get("save_map",False)
        save_qmap = kwargs.get("save_qmap",True)

        # Iterate objects
        D_source   = self.source.get_next()
        D_sample   = self.sample.get_next()
        D_detector = self.detector.get_next()

        # Pull out variables
        nx                  = D_detector["nx"]
        ny                  = D_detector["ny"]
        cx                  = D_detector["cx"]
        cy                  = D_detector["cy"]
        pixel_size          = D_detector["pixel_size"]
        detector_distance   = D_detector["distance"]
        Omega_p             = D_detector["solid_angle_pixel"]
        wavelength          = D_source["wavelength"]

        F_singles = []
        # Calculate patterns of all single particles individually
        for D_particle in D_sample["particles"]:
            p  = D_particle["_class_instance"]
            # Intensity at interaction point
            pos  = D_particle["position"]
            D_particle["intensity"] = self.source.get_intensity(pos,"ph/m2")
            I_0 = D_particle["intensity"]
            # Calculate primary wave amplitude
            # F0 = sqrt(I_0 Omega_p) 2pi/wavelength^2
            F0 = numpy.sqrt(I_0*Omega_p)*2*numpy.pi/wavelength**2
            D_particle["F0"] = F0
            # 3D Orientation
            e0 = D_particle["euler_angle_0"]
            e1 = D_particle["euler_angle_1"]
            e2 = D_particle["euler_angle_2"]

            # UNIFORM SPHERE
            if isinstance(p,ParticleModelSphere):
                # Refractive index
                dn = p.material.get_dn(wavelength)
                # Scattering vectors
                qmap = self.get_qmap(nx=nx, ny=ny, cx=cx, cy=cy, pixel_size=pixel_size, detector_distance=detector_distance, wavelength=wavelength, 
                                     euler_angle_0=0., euler_angle_1=0., euler_angle_2=0.)
                q = numpy.sqrt(qmap[:,:,1]**2+qmap[:,:,2]**2)
                # Intensity scaling factor
                R = D_particle["diameter"]/2.
                V = 4/3.*numpy.pi*R**3
                K = (F0*V*abs(dn))**2
                # Pattern
                F = utils.sphere_diffraction.F_sphere_diffraction(K,q,R)

            # UNIFORM SPHEROID
            elif isinstance(p,ParticleModelSpheroid):
                # Refractive index
                dn = p.material.get_dn(wavelength)
                # Scattering vectors
                qmap = self.get_qmap(nx=nx, ny=ny, cx=cx, cy=cy, pixel_size=pixel_size, detector_distance=detector_distance, wavelength=wavelength, 
                                     euler_angle_0=0., euler_angle_1=0., euler_angle_2=0.)
                qx = qmap[:,:,2]
                qy = qmap[:,:,1]
                # Intensity scaling factor
                R = D_particle["diameter"]/2.
                V = 4/3.*numpy.pi*R**3
                K = (F0*V*abs(dn))**2
                a = utils.spheroid_diffraction.to_spheroid_semi_diameter_a(D_particle["diameter"],D_particle["flattening"])
                c = utils.spheroid_diffraction.to_spheroid_semi_diameter_c(D_particle["diameter"],D_particle["flattening"])
                # Pattern
                # (Rotation is taken into account directly in the diffraction formula (much faster))
                theta = utils.spheroid_diffraction.to_spheroid_theta(euler_angle_0=e0,euler_angle_1=e1,euler_angle_2=e2)
                phi = utils.spheroid_diffraction.to_spheroid_phi(euler_angle_0=e0,euler_angle_1=e1,euler_angle_2=e2)
                F = utils.spheroid_diffraction.F_spheroid_diffraction(K,qx,qy,a,c,theta,phi)

            # MAP
            elif isinstance(p,ParticleModelMap):
                # Refractive index
                dn = p.material.get_dn(wavelength)
                # Scattering vectors
                qmap = self.get_qmap(nx=nx, ny=ny, cx=cx, cy=cy, pixel_size=pixel_size, detector_distance=detector_distance, wavelength=wavelength,
                                     euler_angle_0=e0, euler_angle_1=e1, euler_angle_2=e2)
                # Intensity scaling factor
                R = D_particle["diameter"]/2.
                V = 4/3.*numpy.pi*R**3
                # Generate map
                dx_required  = self.detector.get_real_space_resolution_element(wavelength,cx,cy) / numpy.sqrt(2)
                dx_suggested = self.detector.get_real_space_resolution_element_min(wavelength,cx,cy) / numpy.sqrt(2)
                dn, dx = p.get_map3d(D_particle,dx_required,dx_suggested,dn)
                if save_map:
                    D_particle["dn"] = dn
                    D_particle["dx"] = dx
                    D_particle["dx3"] = dx**3
                # Rescale and shape qmap for nfft
                qmap_scaled = dx * qmap / (2 * numpy.pi)
                qmap_shaped = qmap_scaled.reshape(qmap_scaled.shape[0]*qmap_scaled.shape[1],3)
                    #D_particle["qmap3d"] = detector.generate_qmap_ori(nfft_scaled=True)
                # Check inputs
                invalid_mask = (abs(qmap_shaped)>0.5)
                if (invalid_mask).sum() > 0:
                    qmap_shaped[invalid_mask] = 0.
                    log(logger.debug,"%i invalid pixel positions." % invalid_mask.sum())
                log(logger.debug,"Map3d input shape: (%i,%i,%i), number of dimensions: %i, sum %f" % (dn.shape[0],dn.shape[1],dn.shape[2],len(list(dn.shape)),abs(dn).sum()))
                if (numpy.isfinite(abs(dn))==False).sum() > 0:
                    log(logger.warning,"There are infinite values in the dn map of the object.")
                log(logger.debug,"Scattering vectors shape: (%i,%i); Number of dimensions: %i" % (qmap_shaped.shape[0],qmap_shaped.shape[1],len(list(qmap_shaped.shape))))
                if (numpy.isfinite(qmap_shaped)==False).sum() > 0:
                    log(logger.warning,"There are infinite values in the scattering vectors.")
                # NFFT
                fourier_pattern = utils.nfft.nfft(dn,qmap_shaped)
                # Check output - masking in case of invalid values
                if (invalid_mask).sum() > 0:
                    fourier_pattern[numpy.any(invalid_mask)] = numpy.nan
                # reshaping
                fourier_pattern = numpy.reshape(fourier_pattern,(qmap_scaled.shape[0],qmap_scaled.shape[1]))
                log(logger.debug,"Got pattern of %i x %i pixels." % (fourier_pattern.shape[1],fourier_pattern.shape[0]))
                F = F0 * fourier_pattern * dx**3

            # MOLECULE
            elif isinstance(p,ParticleModelMolecule):
                import spsim
                mol = None
                if D_particle["pdb_filename"] is None:
                    # Write PDB file (not given in configuration)
                    #tmpf_pdb = tempfile.NamedTemporaryFile(mode='w+b', bufsize=-1, suffix='.pdb', prefix='tmp_spsim', dir=None, delete=False)
                    #tmpf_pdb_name = tmpf_pdb.name
                    #tmpf_pdb.close()
                    mol = spsim.alloc_mol()
                    for j,(pos0,pos1,pos2) in zip(D_particle["atomic_number"],D_particle["atomic_position"].reshape(len(D_particle["atomic_number"]),3)):
                        spsim.add_atom_to_mol(mol, int(j), pos0, pos1, pos2)
                    #spsim.write_pdb_from_mol(tmpf_pdb_name, mol)
                    spsim.write_pdb_from_mol("mol.pdbout", mol)
                    #spsim.free_mol(mol)
                    #D_particle["pdb_filename"] = tmpf_pdb_name
                # Start with default spsim configuration
                opts = spsim.set_defaults()
                # Create temporary file for spsim configuration
                tmpf = tempfile.NamedTemporaryFile(mode='w+b', bufsize=-1, suffix='.conf', prefix='tmp_spsim', dir=None, delete=False)
                # Write string sequence from configuration dicts
                spsim_conf = get_spsim_conf(D_source, D_particle, D_detector)
                # Write string sequence to file
                tmpf.writelines(spsim_conf)
                # Close temporary file
                tmpf_name = tmpf.name
                tmpf.close()
                # Read configuration into options struct
                spsim.read_options_file(tmpf_name, opts)
                # This deletes the temporary file
                os.unlink(tmpf_name)
                #spsim.write_options_file("./spsim.confout",opts)
                # Create molecule struct
                if mol is None:
                    mol = spsim.get_molecule(opts)
                if D_particle["atomic_position"] is None:
                    # Get atomic positions and model (extracted from PDB file)
                    pos_img = spsim.sp_image_alloc(mol.natoms,3,1)
                    spsim.array_to_image(mol.pos,pos_img)
                    D_particle["atomic_position"] = pos_img.image.real[:,:].copy()
                    spsim.sp_image_free(pos_img)
                    anum_img = spsim.sp_image_alloc(mol.natoms,1,1)
                    spsim.iarray_to_image(mol.atomic_number,anum_img)
                    D_particle["atomic_number"] = numpy.int32(anum_img.image.real[:,:].copy())
                    spsim.sp_image_free(anum_img)
                # Calculate diffraction pattern
                pat = spsim.simulate_shot(mol, opts)
                # Extract complex Fourier values from spsim output
                F_img = spsim.make_cimage(pat.F,pat.rot,opts)
                phot_img = spsim.make_image(opts.detector.photons_per_pixel,pat.rot,opts)
                F = numpy.sqrt(abs(phot_img.image[:])) * numpy.exp(1.j * numpy.angle(F_img.image[:]))
                spsim.sp_image_free(F_img)
                spsim.sp_image_free(phot_img)
                # Extract qmap from spsim output
                qmap_img = spsim.sp_image_alloc(D_detector["ny"],D_detector["nx"],3)
                spsim.array_to_image(pat.HKL_list, qmap_img)
                qmap = numpy.zeros(shape=(D_detector["ny"],D_detector["nx"],3))
                qmap[:,:,0] = qmap_img.image.real[0,:,:]
                qmap[:,:,1] = qmap_img.image.real[1,:,:]
                qmap[:,:,2] = qmap_img.image.real[2,:,:]
                spsim.sp_image_free(qmap_img)
                spsim.free_diffraction_pattern(pat)
                spsim.free_output_in_options(opts)
                
            else:
                log(logger.error,"No valid particles initialized.")
                sys.exit(0)

            if save_qmap:
                D_particle["qmap"] = qmap

            F_singles.append(F)

        F_tot = numpy.zeros_like(F)
        # Superimpose patterns
        for D_particle,F in zip(D_sample["particles"],F_singles):
            v = D_particle["position"]
            F_tot = F_tot + F * numpy.exp(-1.j*(v[0]*qmap[:,:,0]+v[1]*qmap[:,:,1]+v[2]*qmap[:,:,2])) 
        I_tot, M_tot, IXxX_tot, MXxX_tot = self.detector.detect_photons(abs(F_tot)**2)
        if self.detector.downsampling is not None:
            FXxX_tot, MXxX_tot = utils.resample.downsample(F_tot,self.detector.downsampling,mode="integrate",
                                                           mask2d0=M_tot,bad_bits=PixelMask.PIXEL_IS_IN_MASK,min_N_pixels=1)
        M_tot_binary = M_tot == 0
        MXxX_tot_binary = None if MXxX_tot is None else (MXxX_tot == 0)
        
        O = {}
        O["source"]            = D_source
        O["sample"]            = D_sample
        O["detector"]          = D_detector

        O["fourier_pattern"]   = F_tot
        O["intensity_pattern"] = I_tot
        O["mask_binary"]       = M_tot_binary
        O["mask"]              = M_tot

        if self.detector.downsampling is not None:
            O["fourier_pattern_xxx"]   = FXxX_tot
            O["intensity_pattern_xxx"] = IXxX_tot
            O["mask_xxx"]              = MXxX_tot
            O["mask_xxx_binary"]       = MXxX_tot_binary
            
        return O
        
    def get_qmap(self, nx, ny, cx, cy, pixel_size, detector_distance, wavelength, euler_angle_0, euler_angle_1, euler_angle_2):
        calculate = False
        keys = ["nx","ny","cx","cy","pixel_size","detector_distance","wavelength","euler_angle_0","euler_angle_1","euler_angle_2"]
        if self._qmap_cache == {}:
            calculate = True
        else:
            for k in keys:
                exec "if self._qmap_cache[\"%s\"] != %s: calculate = True" % (k,k)
        if calculate:
            log(logger.info,"Calculating qmap.")
            self._qmap_cache["qmap"] = generate_qmap(nx=nx, ny=ny, cx=cx, cy=cy, pixel_size=pixel_size, detector_distance=detector_distance, wavelength=wavelength,
                                                     euler_angle_0=euler_angle_0,euler_angle_1=euler_angle_1,euler_angle_2=euler_angle_2)
            for k in keys:
                exec "self._qmap_cache[\"%s\"] =  %s" % (k,k)
        return self._qmap_cache["qmap"]          
    
    # ------------------------------------------------------------------------------------------------
    # Caching of map3d might be interesting to implement again in the future
    #def get_map3d(self, map3d, dx, dx_req):
    #    map3d = None
    #    if dx > dx_req:
    #        logger.error("Finer real space sampling required for chosen geometry.")
    #        return
    #    # has map3d_fine the required real space grid?
    #    if map3d == None and abs(self.dX_fine/self.dX-1) < 0.001:
    #        # ok, we'll take the fine map
    #        map3d = self.map3d_fine
    #        logger.debug("Using the fine map for propagtion.")
    #        self._map3d = self.map3d_fine
    #    # do we have an interpolated map?
    #    if map3d == None and self._dX != None:
    #        # does it have the right spacing?
    #        if abs(self._dX/self.dX-1) < 0.001:
    #            # are the shapes of the original fine map and our current fine map the same?
    #            if numpy.all(numpy.array(self.map3d_fine.shape)==numpy.array(self._map3d_fine.shape)):
    #                # is the grid of the original fine map and the current fine map the same?
    #                if self.dX_fine == self._dX_fine:
    #                    # are the values of the original fine map and the cached fine map the same?
    #                    if numpy.all(self.map3d_fine==self._map3d_fine):
    #                        # ok, we take the cached map!
    #                        map3d = self._map3d
    #                        logger.debug("Using the cached interpolated map for propagtion.")
    #    # do we have to do interpolation?
    #    if map3d == None and self.dX_fine < self.dX:
    #        from scipy import ndimage
    #        f = self.dX_fine/self.dX
    #        N_mapfine = self.map3d_fine.shape[0]
    #        L_mapfine = (N_mapfine-1)*self.dX_fine
    #        N_map = int(numpy.floor((N_mapfine-1)*f))+1
    #        L_map = (N_map-1)*self.dX
    #        gt = numpy.float64(numpy.indices((N_map,N_map,N_map)))/float(N_map-1)*(N_mapfine-1)*L_map/L_mapfine
    #        map3d = ndimage.map_coordinates(self.map3d_fine, gt, order=3)
    #        # Cache interpolated data 
    #        self._map3d = map3d
    #        self._dX = N_mapfine/(1.*N_map)*self.dX_fine
    #        # Cace fine data for later decision whether or not the interpolated map can be used again
    #        self._map3d_fine = self.map3d_fine
    #        self._dX_fine = self.dX_fine
    #        logger.debug("Using a newly interpolated map for propagtion.")
    #    return map3d
    # ------------------------------------------------------------------------------------------------

def generate_absqmap(nx,ny,cx,cy,pixel_size,detector_distance,wavelength):
    X,Y = numpy.meshgrid(numpy.arange(nx),
                         numpy.arange(ny))
    # THIS CAST IS VERY IMPORTANT, in python A += B is not the same as A = A + B
    X = numpy.float64(X)
    Y = numpy.float64(Y)
    X -= cx
    Y -= cy
    return utils.scattering_vector.generate_absqmap(X,Y,pixel_size,detector_distance,wavelength)

def generate_qmap(nx,ny,cx,cy,pixel_size,detector_distance,wavelength,euler_angle_0=0.,euler_angle_1=0.,euler_angle_2=0.):
    X,Y = numpy.meshgrid(numpy.arange(nx),
                         numpy.arange(ny))
    # THIS CAST IS VERY IMPORTANT, in python A += B is not the same as A = A + B
    X = numpy.float64(X)
    Y = numpy.float64(Y)
    X -= cx
    Y -= cy
    return utils.scattering_vector.generate_qmap(X,Y,pixel_size,detector_distance,wavelength,euler_angle_0,euler_angle_1,euler_angle_2)
