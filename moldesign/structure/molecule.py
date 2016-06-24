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

import numpy as np

import moldesign as mdt
from moldesign import helpers, utils, data
from moldesign.exceptions import NotCalculatedError
from moldesign import units as u
from moldesign.compute import DummyJob

from . import toplevel, Residue, Chain, Instance, AtomContainer, Bond
from .coord_arrays import *


@toplevel
class MolecularProperties(utils.DotDict):
    """ Stores property values for a molecule.
    These objects will be generally created and updated by EnergyModels, not by users.
    """
    def __init__(self, mol, **properties):
        """Initialization: ``properties`` MUST include positions.

        Args:
            mol (Molecule): molecule that these properties are associated with
            **properties (dict): values of molecular properties (MUST include positions as a key)
        """
        # ADD_FEATURE: always return stored properties in the default unit systems
        super(MolecularProperties, self).__init__(positions=mol.positions.copy(), **properties)

    def geometry_matches(self, mol):
        """Returns:
            bool: True if the molecule's ``position`` is the same as these properties' ``position``
        """
        return np.array_equal(self.positions, mol.positions)


class MolConstraintMixin(object):
    """ Functions for applying and managing geometrical constraints.

    Note:
        This is a mixin class designed only to be mixed into the :class:`Molecule` class. Routines
        are separated are here for code organization only - they could be included in the main
        Molecule class without changing any functionality
    """
    def clear_constraints(self):
        """
        Clear all geometry constraints from the molecule.

        Note:
            This does NOT clear integrator options - such as "constrain H bonds"
        """
        self.constraints = []
        self._reset_methods()

    def constrain_atom(self, atom, pos=None):
        """  Constrain the position of an atom

        Args:
            atom (moldesign.Atom): The atom to constrain
            pos (moldesign.units.MdtQuantity): position to fix this atom at (default: atom.position) [length]

        Returns:
            moldesign.geometry.FixedPosition: constraint object
        """
        from moldesign import geom
        self.constraints.append(geom.FixedPosition(atom, value=pos))
        self._reset_methods()
        return self.constraints[-1]

    def constrain_distance(self, atom1, atom2, dist=None):
        """  Constrain the distance between two atoms

        Args:
            atom1 (moldesign.Atom)
            atom2 (moldesign.Atom)
            dist ([length]): distance value (default: current distance)

        Returns:
            moldesign.geometry.DistanceConstraint: constraint object
        """
        from moldesign import geom
        self.constraints.append(
            geom.constraints.DistanceConstraint(atom1, atom2, value=dist))
        self._reset_methods()
        return self.constraints[-1]

    def constrain_angle(self, atom1, atom2, atom3, angle=None):
        """  Constrain the bond angle atom1-atom2-atom3

        Args:
            atom1 (moldesign.Atom)
            atom2 (moldesign.Atom)
            atom3 (moldesign.Atom)
            angle ([angle]): angle value (default: current angle)

        Returns:
            moldesign.geometry.AngleConstraint: constraint object
        """
        from moldesign import geom
        self.constraints.append(
            geom.constraints.AngleConstraint(atom1, atom2, atom3, value=angle))
        self._reset_methods()
        return self.constraints[-1]

    def constrain_dihedral(self, atom1, atom2, atom3, atom4, angle=None):
        """  Constrain the bond angle atom1-atom2-atom3

        Args:
            atom1 (moldesign.Atom)
            atom2 (moldesign.Atom)
            atom3 (moldesign.Atom)
            atom4 (moldesign.Atom)
            angle ([angle]): angle value (default: current angle)

        Returns:
            moldesign.geometry.AngleConstraint: constraint object
        """
        from moldesign import geom
        self.constraints.append(
            geom.constraints.DihedralConstraint(atom1, atom2, atom3, atom4, value=angle))
        self._reset_methods()
        return self.constraints[-1]


class MolPropertyMixin(object):
    """ Functions for calculating and accessing molecular properties.

    Note:
        This is a mixin class designed only to be mixed into the :class:`Molecule` class. Routines
        are separated are here for code organization only - they could be included in the main
        Molecule class without changing any functionality
    """
    @property
    def mass(self):
        """ u.Scalar[mass]: the molecule's mass
        """
        return sum(self.atoms.mass)

    @property
    def kinetic_energy(self):
        r""" u.Scalar[energy]: Classical kinetic energy :math:`\sum_{\text{atoms}} \frac{p^2}{2m}`
        """
        return helpers.kinetic_energy(self.momenta, self.dim_masses)

    @property
    def kinetic_temperature(self):
        r""" [temperature]: temperature calculated using the equipartition theorem,

        :math:`\frac{2 E_{\text{kin}}}{k_b f}`,

        where :math:`E_{\text{kin}}` is the kinetic energy and :math:`f` is the number of
        degrees of freedom (see :meth:`dynamic_dof <Molecule.dynamic_dof>`)
        """
        return helpers.kinetic_temperature(self.kinetic_energy,
                                           self.dynamic_dof)

    @property
    def dynamic_dof(self):
        """ int: Number of degrees of dynamic freedom
        This can be explicitly set; if not, it will be estimated from the number of constraints on the system
        """
        if self._dof is not None:
            return self._dof
        df = self.ndims
        if self.integrator is not None:
            if self.integrator.params.get('remove_translation', False):
                df -= 3
            if self.integrator.params.get('remove_rotation', False):
                if self.num_atoms > 2:
                    df -= 2
            if 'constraints' in self.integrator.params:
                hbonds = False
                if 'hbonds' in self.integrator.params.constraints:
                    hbonds = True
                    for atom in self.atoms:
                        if atom.atnum == 1: df -= 1
                if 'water' in self.integrator.params.constraints:
                    for residue in self.residues:
                        if residue.type == 'water':
                            if hbonds:
                                df -= 7
                            else:
                                df -= 9

        for constraint in self.constraints:
            df -= constraint.dof
        return df

    @dynamic_dof.setter
    def dynamic_dof(self, val):
        self._dof = val

    @property
    def num_electrons(self):
        """int: The number of electrons in the system, based on the atomic numbers and self.charge"""
        return sum(self.atoms.atnum)-self.charge

    @property
    def homo(self):
        """int: The array index (0-based) of the highest occupied molecular orbital (HOMO).

        Note:
            This assumes a closed shell ground state! """
        return self.num_electrons/2-1

    @property
    def lumo(self):
        """int: The array index (0-based) of the lowest unoccupied molecular orbital (LUMO).

        Note:
            This assumes a closed shell ground state! """
        return self.num_electrons/2


    @property
    def electronic_state(self):
        """ moldesign.orbitals.ElectronicWfn: return the molecule's current electronic state,
        if calculated.

        Raises:
            NotCalculatedError: If the electronic state has not yet been calculated at this
                geometry
        """
        return self.get_property('electronic_state')

    def calc_property(self, name, **kwargs):
        """ Calculate the given property if necessary and return it

        Args:
            name (str): name of the property (e.g. 'potential_energy', 'forces', etc.)

        Returns:
            object: the requested property
        """
        result = self.calculate(requests=[name], **kwargs)
        return result[name]

    def get_property(self, name):
        """ Return the given property if already calculated; raise NotCalculatedError otherwise

        Args:
            name (str): name of the property (e.g. 'potential_energy', 'forces', etc.)

        Raises:
            NotCalculatedError: If the molecular property has not yet been calculated at this
                geometry

        Returns:
            object: the requested property
        """
        if name in self.properties and np.array_equal(self.properties.positions, self.positions):
            return self.properties[name]
        else:
            raise NotCalculatedError(
                "The '%s' property hasn't been calculated yet. " % name,
                "Calculate it with the .calc_%s() method" % name)

    def calculate_forces(self, **kwargs):
        """ Calculate forces and return them

        Returns:
            units.Vector[force]
        """
        return self.calc_property('forces')

    def calculate_potential_energy(self, **kwargs):
        """ Calculate potential energy and return it

        Returns:
            units.Scalar[energy]: potential energy at this position
        """
        return self.calc_property('potential_energy')

    def calculate_dipole(self, **kwargs):
        """ Calculate forces and return them

        Returns:
            units.Vector[length*charge]: dipole moment at this position (len=3)
        """
        return self.calc_property('dipole')

    def calculate_electronic_state(self, **kwargs):
        """ Calculate forces and return them

        Returns:
            moldesign.orbitals.ElectronicWfn: electronic wavefunction object
        """
        return self.calc_property('electronic_state')

    def update_properties(self, properties):
        """
        This is intended mainly as a callback for long-running property calculations.
        When they are finished, they can call this method to update the molecule's properties.

        Args:
            properties (dict): properties-like object. MUST contain a 'positions' attribute.
        """
        if self.properties is None:
            self.properties = properties
        else:
            assert (self.positions == properties.positions).all(), \
                'The molecular geometry does not correspond to these properties'
            self.properties.update()

    @property
    def potential_energy(self):
        """ units.Scalar[energy]: return the molecule's current potential energy, if calculated.

        Raises:
            NotCalculatedError: If the potential energy has not yet been calculated at this
                geometry
        """
        return self.get_property('potential_energy')

    @property
    def forces(self):
        """ units.Vector[force]: return the current force on the molecule, if calculated.

        Raises:
            NotCalculatedError: If the forces have not yet been calculated at this geometry
        """
        return self.get_property('forces')

    @property
    def dipole(self):
        """ units.Vector[length*charge]: return the molecule's dipole moment, if calculated (len=3).

        Raises:
            NotCalculatedError: If the dipole moment has not yet been calculated at this
                geometry
        """
        return self.get_property('dipole')

    @property
    def properties(self):
        """MolecularProperties: Molecular properties calculated at this geometry
        """
        # ADD_FEATURE: some sort of persistent caching so that they aren't lost
        if not self._properties.geometry_matches(self):
            self._properties = MolecularProperties(self)
        return self._properties

    @properties.setter
    def properties(self, val):
        """ Sanity checks - make sure that these properties correspond to the correct geoemtry.
        """
        assert val.geometry_matches(self), \
            "Can't set properties - they're for a different molecular geometry"
        self._properties = val

    # synonyms for backwards compatibility
    calc_electronic_state = calculate_electronic_state
    calc_dipole = calculate_dipole
    calc_potential_energy = calculate_potential_energy
    calc_forces = calculate_forces


class MolDrawingMixin(object):
    """ Methods for visualizing molecular structure.

    See Also:
        :class:`moldesign.structure.atomcollections.AtomContainer`

    Note:
        This is a mixin class designed only to be mixed into the :class:`Molecule` class. Routines
        are separated are here for code organization only - they could be included in the main
        Molecule class without changing any functionality
    """
    def draw(self, **kwargs):
        """ Visualize this molecule (Jupyter only).

        Creates a 3D viewer, and, for small molecules, a 2D viewer).

        Args:
            width (int): width of the viewer in pixels
            height (int): height of the viewer in pixels

        Returns:
            moldesign.ui.SelectionGroup
        """
        from moldesign import widgets
        if self.is_small_molecule:
            return super(Molecule, self).draw(**kwargs)
        else:
            return widgets.base.SelectionGroup([self.draw3d(),
                                         uibase.components.AtomInspector()])

    def draw_orbitals(self, **kwargs):
        """ Visualize any calculated molecular orbitals (Jupyter only).

        Returns:
            mdt.orbitals.OrbitalViewer
        """
        from moldesign.widgets.orbitals import OrbitalViewer
        return OrbitalViewer(self, **kwargs)


class MolReprMixin(object):
    """ Methods for creating text-based representations of the molecule

    Note:
        This is a mixin class designed only to be mixed into the :class:`Molecule` class. Routines
        are separated are here for code organization only - they could be included in the main
        Molecule class without changing any functionality
    """
    def __repr__(self):
        try:
            return '<%s (%s), %d atoms>' % (self.name,
                                            self.__class__.__name__,
                                            len(self.atoms))
        except:
            return '<molecule (error in __repr__) at %s>' % id(self)

    def __str__(self):
        return 'Molecule: %s' % self.name

    def markdown_summary(self):
        """A markdown description of this molecule.

        Returns:
            str: Markdown"""
        # TODO: remove leading underscores for descriptor-protected attributes
        lines = ['### Molecule: "%s" (%d atoms)' % (self.name, self.natoms),
                 '**Mass**: {:.2f}'.format(self.mass),
                 '**Formula**: %s' % self.get_stoichiometry(html=True),
                 '**Potential model**: %s' % str(self.energy_model),
                 '**Integrator**: %s' % self.integrator]

        if self.is_biomolecule:
            lines.extend(self.biomol_summary_markdown())

        return '\n\n'.join(lines)

    def _repr_markdown_(self):
        return self.markdown_summary()

    def biomol_summary_markdown(self):
        """A markdown description of biomolecular structure.

        Returns:
            str: Markdown string"""
        lines = []
        if len(self.residues) > 1:
            table = self.get_residue_table()
            lines.append('### Residues')
            # extra '|' here may be workaround for a bug in ipy.markdown?
            lines.append(table.markdown(replace={0: ' '}) + '|')

            lines.append('### Chains')
            seqs = []
            for chain in self.chains:
                seq = chain.sequence
                # deal with extra-long sequences
                seqstring = []
                for i in xrange(0, len(seq), 80):
                    seqstring.append(seq[i:i + 80])
                seqstring = '\n'.join(seqstring)
                seqs.append('**%s**: `%s`' % (chain.name, seqstring))
            lines.append('<br>'.join(seqs))
        return lines

    def get_residue_table(self):
        """Creates a data table summarizing this molecule's primary structure.

        Returns:
            moldesign.utils.MarkdownTable"""
        table = utils.MarkdownTable(*(['chain'] + data.RESTYPES.keys()))
        for chain in self.chains:
            counts = {}
            unk = []
            for residue in chain.residues:
                cat = residue.type
                if cat == 'unknown':
                    unk.append(residue.name)
                counts[cat] = counts.get(cat, 0) + 1
            counts['chain'] = '<pre><b>%s</b></pre>' % chain.name
            if 0 < len(unk) <= 4:
                counts['unknown'] = ','.join(unk)
            table.add_line(counts)
        return table

    def get_stoichiometry(self, html=False):
        """ Return this molecule's stoichiometry

        Returns:
            str
        """
        counts = {}
        for symbol in self.atoms.symbol:
            counts[symbol] = counts.get(symbol, 0) + 1

        my_elements = sorted(counts.keys())
        if html: template = '%s<sub>%d</sub>'
        else: template = '%s%d'
        return ''.join([template % (k, counts[k]) for k in my_elements])


class MolTopologyMixin(object):
    """ Functions for building and keeping track of bond topology and biochemical structure.

    Note:
        This is a mixin class designed only to be mixed into the :class:`Molecule` class. Routines
        are separated are here for code organization only - they could be included in the main
        Atom class without changing any functionality
    """
    def assert_atom(self, atom):
        """If passed an integer, just return self.atoms[atom].
         Otherwise, assert that the atom belongs to this molecule"""
        if type(atom) is int:
            atom = self.mol.atoms[atom]
        else:
            assert atom.parent is self, "Atom %s does not belong to %s" % (atom, self)
        return atom

    def _rebuild_topology(self, bond_graph=None):
        """ Build the molecule's bond graph based on its atoms' bonds

        Args:
            bond_graph (dict): graph to build the bonds from
        """
        if bond_graph is None:
            self.bond_graph = self._build_bonds(self.atoms)
        else:
            self.bond_graph = bond_graph

        self.is_biomolecule = False
        self.ndims = 3 * self.num_atoms
        self._positions = np.zeros(self.ndims) * u.default.length
        self._momenta = np.zeros(self.ndims) * u.default.momentum
        self.dim_masses = np.zeros(self.ndims) * u.default.mass
        self._assign_atom_indices()
        self._assign_residue_indices()
        self._dof = None
        num_bonds = 0
        for atom in self.bond_graph:
            num_bonds += len(atom.bond_graph)
        assert num_bonds % 2 == 0
        self.num_bonds = num_bonds / 2

    @staticmethod
    def _build_bonds(atoms):
        """ Build a bond graph describing bonds between this list of atoms

        Args:
            atoms (List[moldesign.atoms.Atom])
        """
        # TODO: check atom parents
        bonds = {}

        # First pass - create initial bonds
        for atom in atoms:
            assert atom not in bonds, 'Atom appears twice in this list'
            if hasattr(atom, 'bonds') and atom.bond_graph is not None:
                bonds[atom] = atom.bond_graph
            else:
                bonds[atom] = {}

        # Now make sure both atoms have a record of their bonds
        for atom in atoms:
            for nbr in bonds[atom]:
                if atom in bonds[nbr]:
                    assert bonds[nbr][atom] == bonds[atom][nbr]
                else:
                    bonds[nbr][atom] = bonds[atom][nbr]
        return bonds

    def _assign_atom_indices(self):
        """
        Create geometry-level information based on constituent atoms, and mark the atoms
        as the property of this molecule
        """
        idim = 0
        for idx, atom in enumerate(self.atoms):
            atom._set_parent(self)
            atom.index = idx
            atomslice = slice(idim, idim + 3)
            atom.parent_slice = atomslice
            idim += 3
            self.dim_masses[atomslice] = atom.mass
            # Here, we index the atom arrays directly into the molecule
            atom._index_into_molecule('_position', self.positions, atomslice)
            atom._index_into_molecule('_momentum', self.momenta, atomslice)

    def _assign_residue_indices(self):
        """
        Set up the chain/residue/atom hierarchy
        """
        # TODO: consistency checks

        if self._defchain is None:
            self._defchain = Chain(name='Z',
                                   index=99,
                                   parent=None)

        if self._defres is None:
            self._defres = Residue(name='UNK999',
                                   index=999,
                                   pdbindex=1,
                                   pdbname='UNK',
                                   chain=self._defchain,
                                   parent=None)
            self._defchain.add(self._defres)

        default_residue = self._defres
        default_chain = self._defchain
        num_biores = 0

        for atom in self.atoms:
            # if atom has no chain/residue, assign defaults
            if atom.residue is None:
                atom.residue = default_residue
                atom.chain = default_chain
                atom.residue.add(atom)

            # assign the chain to this molecule if necessary
            if atom.chain.parent is None:
                atom.chain.parent = self
                atom.chain.index = len(self.chains)

                assert atom.chain.name not in self.chains
                self.chains.add(atom.chain)
            else:
                assert atom.chain.parent is self

            # assign the residue to this molecule
            if atom.residue.parent is None:
                atom.residue.parent = self
                atom.residue.index = len(self.residues)
                self.residues.append(atom.residue)
                if atom.residue.type in ('dna', 'rna', 'protein'): num_biores += 1
            else:
                assert atom.chain.parent is self

        self.is_biomolecule = (num_biores >= 2)
        self.nchains = self.n_chains = self.num_chains = len(self.chains)
        self.nresidues = self.n_residues = self.num_residues = len(self.residues)


@toplevel
class Molecule(AtomContainer,
               MolConstraintMixin,
               MolPropertyMixin,
               MolDrawingMixin,
               MolReprMixin,
               MolTopologyMixin):
    """
    This is moldesign's principal object. It stores a list of atoms in 3D space
    and handles interfaces with energy and dynamics models.


    Args:
        atomcontainer (AtomContainer or AtomList or List[moldesign.Atom]): atoms that make up
            this molecule.

            Note:
                If the passed atoms don't already belong to a molecule, they will be assigned
                to this one. If they DO already belong to a molecule, they will be copied,
                leaving the original molecule untouched.

        name (str): name of the molecule (automatically generated if not provided)
        bond_graph (dict): dictionary specifying bonds between the atoms - of the form
            ``{atom1:{atom2:bond_order, atom3:bond_order}, atom2:...}``
            This structure must be symmetric; we require
            ``bond_graph[atom1][atom2] == bond_graph[atom2][atom1]``
        energy_model (moldesign.models.base.EnergyModelBase): Object that drives calculation of
            molecular properties, driven by `mol.calculate()`
        integrator (moldesign.integrators.base.IntegratorBase): Object that drives movement of 3D
            coordinates in time, driven by mol.run()
        copy_atoms (bool): Create the molecule with *copies* of the passed atoms
            (they will be copied automatically if they already belong to another molecule)
        pdbname (str): Name of the PDB file
        charge (units.Scalar[charge]): molecule's formal charge



    Attributes:
        atoms
        bond_graph (dict): symmetric dictionary specifying bonds between the
           atoms:

               ``bond_graph = {atom1:{atom2:bond_order, atom3:bond_order}, atom2:...}``

               ``bond_graph[atom1][atom2] == bond_graph[atom2][atom1]``
        residues (List[moldesign.Residue]): flat list of all biomolecular residues in this molecule
        chains (Dict[moldesign.Chain]): Biomolecular chains - individual chains can be
            accessed as ``mol.chains[list_index]`` or ``mol.chains[chain_name]``
        name (str): A descriptive name for molecule
        charge (units.Scalar[charge]): molecule's formal charge
        ndims (int): length of the positions, momenta, and forces arrays (usually 3*self.num_atoms)
        num_atoms (int): number of atoms (synonyms: num_atoms, numatoms)
        positions (units.Vector[length]): flat array of atomic positions, len=`self.ndims` [length]
        momenta (units.Vector[momentum]): flat array of atomic momenta, len=`self.ndims`
        time (units.Scalar[time]): current time in dynamics
        energy_model (moldesign.models.base.EnergyModelBase): Object that calculates
            molecular properties - driven by `mol.calculate()`
        integrator (moldesign.integrators.base.IntegratorBase): Object that drives movement of 3D
            coordinates in time, driven by mol.run()
        is_biomolecule (bool): True if this molecule contains at least 2 biochemical residues
    """

    # TODO: UML diagrams, describe structure
    positions = ProtectedArray('_positions')
    momenta = ProtectedArray('_momenta')

    def __init__(self, atomcontainer,
                 name=None, bond_graph=None,
                 energy_model=None,
                 integrator=None,
                 copy_atoms=False,
                 pdbname=None,
                 charge=0):
        # NEW_FEATURE: deal with random number generators per-geometry
        # NEW_FEATURE: per-geometry output logging
        super(Molecule, self).__init__()

        # copy atoms from another object (i.e., a molecule)
        oldatoms = helpers.get_all_atoms(atomcontainer)

        if copy_atoms or (oldatoms[0].parent is not None):
            print 'INFO: Copying atoms into new molecule'
            atoms = oldatoms.copy()
            if name is None:  # Figure out a reasonable name
                if oldatoms[0].parent is not None:
                    name = oldatoms[0].parent.name + ' copy'
                elif hasattr(atomcontainer, 'name') and isinstance(atomcontainer.name, str):
                    name = utils.if_not_none(name, atomcontainer.name + ' copy')
                else:
                    name = 'unnamed'
        else:
            atoms = oldatoms

        self.atoms = atoms
        self.time = None
        self.name = 'uninitialized molecule'
        self._defres = None
        self._defchain = None
        self.pdbname = pdbname
        self.charge = charge
        self.constraints = []

        # Builds the internal memory structures
        self.chains = Instance(parent=self)
        self.residues = []
        self._rebuild_topology(bond_graph=bond_graph)

        if name is not None:
            self.name = name
        elif not self.is_small_molecule:
            self.name = 'unnamed macromolecule'
        else:
            self.name = self.get_stoichiometry()
        if energy_model:
            self.set_energy_model(energy_model)
        else:
            self.energy_model = None
        if integrator:
            self.set_integrator(integrator)
        else:
            self.integrator = None
        self._properties = MolecularProperties(self)
        self.ff = utils.DotDict()


    # TODO: underscores or not? Buckyball needs a global rule
    def newbond(self, a1, a2, order):
        """ Create a new bond

        Args:
            a1 (moldesign.Atom): First atom in the bond
            a2 (moldesign.Atom): Second atom in the bond
            order (int): order of the bond

        Returns:
            moldesign.Bond
        """
        # TODO: this should signal to the energy model that the bond structure has changed
        assert a1.parent == a2.parent == self
        return a1.bond_to(a2, order)

    def addatom(self, newatom):
        """  Add a new atom to the molecule

        Args:
            newatom (moldesign.Atom): The atom to add (it will be copied if it already belongs to a molecule
        """
        self.addatoms([newatom])

    def addatoms(self, newatoms):
        """Add new atoms to this molecule.
        For now, we really just rebuild the entire molecule in place.

        Args:
           newatoms (List[moldesign.Atom]))
        """
        self._reset_methods()

        for atom in newatoms: assert atom.parent is None
        self.atoms.extend(newatoms)

        # symmetrize bonds between the new atoms and the pre-existing molecule
        bonds = self._build_bonds(self.atoms)
        for newatom in newatoms:
            for nbr in bonds[newatom]:
                if nbr in self.bond_graph:  # i.e., it's part of the original molecule
                    bonds[nbr][newatom] = bonds[newatom][nbr]

        self._rebuild_topology(bonds)

    def deletebond(self, bond):
        """ Remove this bond from the molecule's topology
        """
        self.bond_graph[bond.a1].pop(bond.a2)
        self.bond_graph[bond.a2].pop(bond.a1)

    def run(self, run_for):
        """ Starts the integrator's default integration

        Args:
            run_for (int or [time]): number of steps or amount of time to run for

        Returns:
            moldesign.trajectory.Trajectory
        """
        return self.integrator.run(run_for)

    def calculate(self, requests=None, wait=True, use_cache=True):
        """
        Runs a potential energy calculation on the current geometry, returning the requested quantities.
        If `requests` is not passed, the properties specified in the energy_models DEFAULT_PROPERTIES
        will be calculated.

        Args:
            requests (List[str]): list of quantities for the model to calculate,
                e.g. ['dipole', 'forces']
            wait (bool): if True, wait for the calculation to complete before returning. \
                 If false, return a job object - this will not update the molecule's properties!
            use_cache (bool): Return cached results if possible

        Returns:
            MolecularProperties
        """
        if requests is None: requests = []

        # Figure out what needs to be calculated,
        # and either launch the job or set the result
        to_calculate = set(requests + self.energy_model.DEFAULT_PROPERTIES)
        if use_cache:
            to_calculate = to_calculate.difference(self.properties)
        if len(to_calculate) == 0:
            job = self.properties
        else:
            job = self.energy_model.calculate(to_calculate)

        if wait:
            # We'll wait for the job to complete, then
            # returns the molecule's calculated properties
            if hasattr(job, 'wait'):
                job.wait()
                properties = job.result
            else:
                properties = job
            self.properties.update(properties)
            return self.properties
        else:
            # We're not waiting for the job to complete - return a job object
            if hasattr(job, 'wait'):
                return job
            else:
                return DummyJob(job)

    def set_energy_model(self, model):
        """ Associate an energy model with this molecule

        Args:
            model (moldesign.methods.EnergyModelBase):
        """
        self.energy_model = model
        self.properties = MolecularProperties(self)
        model.mol = self
        if 'charge' in model.params:
            if model.params.charge is None:
               model.params.charge = self.charge
            elif model.params.charge != self.charge:
                print "Warning: molecular charge (%d) does not match energy model's charge (%d)" % (
                    self.charge, model.params.charge)
        model._prepped = False

    def minimize(self, assert_converged=False, **kwargs):
        """ Run a minimization based on the potential model.
        If force_tolerance is not specified, the program defaults are used.
        If specified, the largest force component must be less than force_tolerance
        and the RMSD must be less than 1/3 of it. (based on GAMESS OPTTOL keyword)

        Args:
            nsteps (int): max number of steps before exiting
            frame_interval (int): Number of steps per frame of minimization trajectory
            force_tolerance ([force]): Force threshold for convergence as described above.
            assert_converged (bool): Raise an exception if the minimization does not converged.

        Returns:
            moldesign.trajectory.Trajectory
        """
        trajectory = self.energy_model.minimize(**kwargs)
        print 'Reduced energy from %s to %s' % (trajectory.potential_energy[0],
                                                trajectory.potential_energy[-1])
        if assert_converged:
            raise NotImplementedError()

        return trajectory

    def _force_converged(self, tolerance):
        """ Return True if the forces on this molecule:
        1) Are less than tolerance in every dimension
        2) have an RMS of less than 1/3 the tolerance value

        Args:
            tolerance ([force]): force tolerance

        Returns:
            bool
        """
        forces = self.calc_forces()
        if forces.max() > tolerance: return False
        rmsd2 = forces.dot(forces) / self.ndims
        if rmsd2 > tolerance * tolerance / 3.0: return False
        return True

    def set_integrator(self, integrator):
        """ Associate an integrator with this molecule

        Args:
            integrator (moldesign.methods.IntegratorBase):
        """
        self.integrator = integrator
        integrator.mol = self
        integrator._prepped = False

    def write(self, filename=None, **kwargs):
        """ Write this molecule to a string or file.

        This is a convenience method for :ref:`moldesign.converters.write`

        Args:
            filename (str): filename to write (if not passed, write to string)
            format (str): file format (if filename is not passed, format must be specified)
                Guessed from file extension if not passed
        """
        # TODO: make it easier to do the right thing, which is write to .pkl.bz2
        return mdt.write(self, filename=filename, **kwargs)

    @property
    def is_small_molecule(self):
        """bool: True if molecule's mass is less than 500 Daltons (not mutually exclusive with
        :meth:`self.is_biomolecule <Molecule.is_biomolecule>`)"""
        return sum(self.atoms.mass) <= 500.0 * u.amu

    @property
    def bonds(self):
        """ Iterator over all bonds in the molecule

        Yields:
            moldesign.atoms.Bond: bond object
        """
        for atom in self.bond_graph:
            for nbr in self.bond_graph[atom]:
                if atom.index > nbr.index: continue  # don't double count
                yield Bond(atom, nbr)

    def _reset_methods(self):
        """
        Called whenever a property is changed that the energy model and/or integrator need to know about
        """
        # TODO: what should this do with the property object?
        if self.energy_model is not None:
            self.energy_model._prepped = False
        if self.integrator is not None:
            self.integrator._prepped = False


