# Copyright 2016 Autodesk Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys

import IPython.display as dsp
import numpy as np

import moldesign as mdt
from moldesign import units as u
from moldesign import utils
from moldesign.helpers import VolumetricGrid, colormap
from nbmolviz.drivers3d import MolViz_3DMol
from . import toplevel, ColorMixin


# Right now hard-coded to use 3DMol driver - will need to be configurable when there's more than one
# In theory, the only tie to the specific driver should be the class that we inherit from
@toplevel
class GeometryViewer(MolViz_3DMol, ColorMixin):
    """
    Viewer for static and multiple-frame geometries

    :ivar mol: Buckyball molecule
    :type mol: moldesign.Molecule.molecule
    """
    DISTANCE_UNITS = u.angstrom
    HIGHLIGHT_COLOR = '#1FF3FE'
    DEFAULT_COLOR_MAP = colormap
    DEFAULT_WIDTH = 625
    DEFAULT_HEIGHT = 400
    DEF_PADDING = 2.25 * u.angstrom

    def __reduce__(self):
        """prevent these from being pickled for now"""
        return utils.make_none, tuple()

    def __init__(self, mol=None, style=None, display=False, **kwargs):
        """
        TODO: make clickable atoms work with trajectories
        TODO: coloring methods

        Args:
            mol (moldesign.AtomContainer): Atoms to visualize
        """
        kwargs.setdefault('height', self.DEFAULT_HEIGHT)
        kwargs.setdefault('width', self.DEFAULT_WIDTH)
        super(GeometryViewer, self).__init__(**kwargs)
        self.selection_group = None
        self.selection_id = None
        self.atom_highlights = []
        self.frame_change_callback = None
        self.wfns = []
        self._cached_orbitals = set()
        self._axis_objects = None
        self._frame_positions = []
        self._colored_as = {}
        if mol:
            self.add_molecule(mol)
            self._frame_positions.append(self.get_positions())
            if style is None:
                self.autostyle()
            else:
                self.set_style(style)
        if display: dsp.display(self)

    @property
    def wfn(self):
        return self.wfns[self.current_frame]

    def autostyle(self):
        if self.mol.mass <= 500.0 * u.dalton:
            self.stick()
        else:
            cartoon_atoms = []
            line_atoms = []
            stick_atoms = []
            biochains = set()
            for residue in self.mol.residues:
                if residue.type in ('protein', 'dna', 'rna'):
                    biochains.add(residue.chain)

                if residue.type == 'protein':
                    cartoon_atoms.extend(residue.atoms)
                elif residue.type in ('water', 'solvent'):
                    line_atoms.extend(residue.atoms)
                elif residue.type in ('dna', 'rna') and self.mol.num_atoms > 1000:
                    cartoon_atoms.extend(residue.atoms)
                else:  # includes DNA, RNA if molecule is small enough
                    stick_atoms.extend(residue.atoms)

            if cartoon_atoms:
                self.cartoon(atoms=cartoon_atoms)
                if len(biochains) > 1:
                    self.color_by('chain', atoms=cartoon_atoms)
                else:
                    self.color_by('residue.resname', atoms=cartoon_atoms)
            if line_atoms:
                self.line(atoms=line_atoms)
            if stick_atoms:
                self.stick(atoms=stick_atoms)

        # Deal with unbonded atoms (they only show up in VDW rep)
        if self.mol.num_atoms < 1000:
            lone = [atom for atom in self.mol.atoms if atom.num_bonds == 0]
            if lone:
                self.vdw(atoms=lone, render=False, radius=0.5)

    def show_unbonded(self, radius=0.5):
        lone = [atom for atom in self.mol.atoms if atom.num_bonds == 0]
        if lone: self.vdw(atoms=lone, radius=radius)

    @staticmethod
    def _atoms_to_json(atomlist):
        if hasattr(atomlist, 'iteratoms'):
            idxes = [a.index for a in atomlist.iteratoms()]
        else:
            # TODO: verify that these are actually atoms and not something else with a .index
            idxes = [a.index for a in atomlist]
        atomsel = {'index': idxes}
        return atomsel

    @utils.doc_inherit
    def set_color(self, color, atoms=None, _store=True):
        if _store:
            for atom in utils.if_not_none(atoms, self.mol.atoms):
                self._colored_as[atom] = color
        return super(GeometryViewer, self).set_color(color, atoms=atoms)

    @utils.doc_inherit
    def set_colors(self, colormap, _store=True):
        if _store:
            for color, atoms in colormap.iteritems():
                for atom in atoms:
                    self._colored_as[atom] = color
        return super(GeometryViewer, self).set_colors(colormap)

    @utils.doc_inherit
    def unset_color(self, atoms=None, _store=True):
        if _store:
            for atom in utils.if_not_none(atoms, self.mol.atoms):
                self._colored_as.pop(atom, None)

        result = super(GeometryViewer, self).unset_color(atoms=atoms)
        if self.atom_highlights: self._redraw_highlights()
        return result

    def get_input_file(self):
        if len(self.mol.atoms) <= 250:
            fmt = 'sdf'
        else:
            fmt = 'pdb'
        if not hasattr(self.mol, 'write'):
            writemol = mdt.Molecule(self.mol)
        else:
            writemol = self.mol
        instring = writemol.write(format=fmt)
        return instring, fmt

    def get_positions(self):
        positions = [atom.position.value_in(self.DISTANCE_UNITS).tolist()
                     for atom in self.mol.atoms]
        return positions

    def calc_orb_grid(self, orbname, npts, framenum):
        """ Calculate orbitals on a grid

        Args:
            orbname: Either orbital index (for canonical orbitals) or a tuple ( [orbital type],
               [orbital index] ) where [orbital type] is a keyword (e.g., canonical, natural, nbo,
               ao, etc)
            npts (int): resolution in each dimension of the grid
            framenum (int): wavefunction for which frame number?

        Returns:
            moldesign.viewer.VolumetricGrid: orbital values on a grid
        """
        # NEWFEATURE: limit grid size based on the non-zero atomic centers. Useful for localized
        #    orbitals, which otherwise require high resolution
        try:
            orbtype, orbidx = orbname
        except TypeError:
            orbtype = 'canonical'
            orbidx = orbname

        # self.wfn should already be set to the wfn for the current frame
        orbital = self.wfns[framenum].orbitals[orbtype][orbidx]
        positions = self.wfns[framenum].positions
        grid = VolumetricGrid(positions,
                              padding=self.DEF_PADDING,
                              npoints=npts)
        coords = grid.xyzlist().reshape(3, grid.npoints ** 3).T
        values = orbital(coords)
        grid.fxyz = values.reshape(npts, npts, npts)

        return grid

    def get_orbnames(self):
        raise NotImplementedError()

    def draw_atom_vectors(self, vecs, rescale_to=1.75,
                          scale_factor=None, opacity=0.85,
                          radius=0.11, **kwargs):
        """
        For displaying atom-centered vector data (e.g., momenta, forces)
        :param rescale_to: rescale to this length (in angstroms) (not used if scale_factor is passed)
        :param scale_factor: Scaling factor for arrows: dimensions of [vecs dimensions] / [length]
        :param kwargs: keyword arguments for self.draw_arrow
        """
        kwargs['radius'] = radius
        kwargs['opacity'] = opacity
        if vecs.shape == (self.mol.ndims,):
            vecs = vecs.reshape(self.mol.num_atoms, 3)
        assert vecs.shape == (self.mol.num_atoms, 3), '`vecs` must be a num_atoms X 3 matrix or' \
                                                      ' 3*num_atoms vector'
        assert not np.allclose(vecs, 0.0), "Input vectors are 0."

        # strip units and scale the vectors appropriately
        if scale_factor is not None:  # scale all arrows by this quantity
            if (u.get_units(vecs)/scale_factor).dimensionless:  # allow implicit scale factor
                scale_factor = scale_factor / self.DISTANCE_UNITS

            vecarray = vecs / scale_factor
            try: arrowvecs = vecarray.value_in(self.DISTANCE_UNITS)
            except AttributeError: arrowvecs = vecarray

        else:  # rescale the maximum length arrow length to rescale_to
            try:
                vecarray = vecs.magnitude
                unit = vecs.getunits()
            except AttributeError:
                vecarray = vecs
                unit = ''
            lengths = np.sqrt((vecarray * vecarray).sum(axis=1))
            scale = (lengths.max() / rescale_to)  # units of [vec units] / angstrom
            if hasattr(scale,'defunits'): scale = scale.defunits()
            arrowvecs = vecarray / scale
            print 'Arrow scale: {q:.3f} {unit} per {native}'.format(q=scale, unit=unit,
                                                                    native=self.DISTANCE_UNITS)
        shapes = []
        for atom, vecarray in zip(self.mol.atoms, arrowvecs):
            if vecarray.norm() < 0.2: continue
            shapes.append(self.draw_arrow(atom.position, vector=vecarray, **kwargs))
        return shapes

    def draw_axis(self, on=True):
        label_kwargs = dict(color='white', opacity=0.4, fontsize=14)
        if on and self._axis_objects is None:
            xarrow = self.draw_arrow([0, 0, 0], [1, 0, 0], color='red')
            xlabel = self.draw_label([1.0, 0.0, 0.0], text='x', **label_kwargs)
            yarrow = self.draw_arrow([0, 0, 0], [0, 1, 0], color='green')
            ylabel = self.draw_label([-0.2, 1, -0.2], text='y', **label_kwargs)
            zarrow = self.draw_arrow([0, 0, 0], [0, 0, 1], color='blue')
            zlabel = self.draw_label([0, 0, 1], text='z', **label_kwargs)
            self._axis_objects = [xarrow, yarrow, zarrow,
                                  xlabel, ylabel, zlabel]

        elif not on and self._axis_objects is not None:
            for arrow in self._axis_objects:
                self.remove(arrow)
            self._axis_objects = None

    def draw_forces(self, **kwargs):
        return self.draw_atom_vectors(self.mol.forces, **kwargs)

    def draw_momenta(self, **kwargs):
        return self.draw_atom_vectors(self.mol.momenta, **kwargs)

    def highlight_atoms(self, atoms=None):
        """

        Args:
            atoms (list[Atoms]): list of atoms to highlight. If None, remove all highlights
        """
        # TODO: Need to handle style changes
        if self.atom_highlights:  # first, unhighlight old highlights
            to_unset = []
            for atom in self.atom_highlights:
                if atom in self._colored_as: self.set_color(atoms=[atom],
                                                            color=self._colored_as[atom],
                                                            _store=False)
                else:
                    to_unset.append(atom)

            if to_unset:
                self.atom_highlights = []
                self.unset_color(to_unset, _store=False)

        self.atom_highlights = utils.if_not_none(atoms, [])
        self._redraw_highlights()

    def _redraw_highlights(self):
        if self.atom_highlights:
            self.set_color(self.HIGHLIGHT_COLOR, self.atom_highlights, _store=False)

    def append_frame(self, positions=None, wfn=None):
        # override base method - we'll handle frames entirely in python
        # this is copied verbatim from molviz, except for the line noted
        if positions is None:
            positions = self.get_positions()

        positions = self._convert_units(positions)
        try:
            positions = positions.tolist()
        except AttributeError:
            pass

        self.num_frames += 1
        self._frame_positions.append(positions)  # only modification from molviz
        self.wfns.append(wfn)
        self.show_frame(self.num_frames - 1)

    @utils.doc_inherit
    def show_frame(self, framenum, _fire_event=True, update_orbitals=True):
        # override base method - we'll handle frames using self.set_positions
        # instead of any built-in handlers
        if framenum != self.current_frame:
            self.set_positions(self._frame_positions[framenum])
            self.current_frame = framenum
            if _fire_event and self.selection_group:
                self.selection_group.update_selections(self, {'framenum': framenum})
            if update_orbitals:
                self.redraw_orbs()

    def handle_selection_event(self, selection):
        """
        Deals with an external selection event
        :param selection:todo
        :return:
        """
        orb_changed = False
        if 'atoms' in selection:
            self.highlight_atoms(selection['atoms'])

        if 'framenum' in selection:
            if self.frame_change_callback is not None:
                self.frame_change_callback(selection['framenum'])
            self.show_frame(selection['framenum'],
                            _fire_event=False,
                            update_orbitals=False)
            orb_changed = True

        if ('orbname' in selection) and (selection['orbname'] != self.current_orbital):
            orb_changed = True
            self.current_orbital = selection['orbname']

        isoval = self.orbital_spec['isoval'] if 'isoval' in self.orbital_spec else None

        if ('orbital_isovalue' in selection) and (
                    selection['orbital_isovalue'] != isoval):
            orb_changed = True
            self.orbital_spec['isoval'] = selection['orbital_isovalue']

        # redraw the orbital if necessary
        if orb_changed:
            self.redraw_orbs()

    def redraw_orbs(self):
        if self.orbital_is_selected:
            self.draw_orbital(self.current_orbital, **self.orbital_spec)

    @property
    def orbital_is_selected(self):
        return self.current_orbital is not None and self.current_orbital[1] is not None

    def _convert_units(self, obj):
        try:
            return obj.value_in(self.DISTANCE_UNITS)
        except AttributeError:
            return obj
