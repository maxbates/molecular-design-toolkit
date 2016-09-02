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
from __future__ import absolute_import  # prevent clashes between this and the "pyscf" package

from cStringIO import StringIO

from moldesign import units as u, compute, orbitals
from moldesign.interfaces.pyscf_interface import force_remote, mol_to_pyscf, \
    StatusLogger, SPHERICAL_NAMES
from .base import QMBase
from moldesign import uibase
from moldesign.utils import DotDict


def exports(o):
    __all__.append(o.__name__)
    return o
__all__ = []


class LazyClassMap(object):
    """ For lazily importing classes from modules (when there's a lot of import overhead)

    Class names should be stored as their *absolute import strings* so that they can be imported
    only when needed

    Example:
        >>> myclasses = LazyClassMap({'od': 'collections.OrderedDict'})
        >>> myclasss['od']()
        OrderedDict()
    """
    def __init__(self, mapping):
        self.mapping = mapping

    def __getitem__(self, key):
        import importlib
        fields = self.mapping[key].split('.')
        cls = fields[-1]
        modname = '.'.join(fields[:-1])
        mod = importlib.import_module(modname)
        return getattr(mod, cls)

# PySCF metadata constants
THEORIES = LazyClassMap({'hf': 'pyscf.scf.RHF', 'rhf': 'pyscf.scf.RHF',
                         'uhf': 'pyscf.scf.UHF',
                         'mcscf': 'pyscf.mcscf.CASSCF', 'casscf': 'pyscf.mcscf.CASSCF',
                         'casci': 'pyscf.mcscf.CASCI',
                         'mp2': 'pyscf.mp.MP2',
                         'dft': 'pyscf.dft.RKS', 'rks': 'pyscf.dft.RKS', 'ks': 'pyscf.dft.RKS'})

NEEDS_REFERENCE = set('mcscf casscf casci mp2'.split())
NEEDS_FUNCTIONAL = set('dft rks ks uks'.split())
IS_SCF = set('rhf uhf hf casscf mcscf dft rks ks'.split())
FORCE_CALCULATORS = LazyClassMap({'rhf': 'pyscf.grad.RHF', 'hf': 'pyscf.grad.RHF'})


@exports
class PySCFPotential(QMBase):
    DEFAULT_PROPERTIES = ['potential_energy',
                          'wfn',
                          'mulliken']
    ALL_PROPERTIES = DEFAULT_PROPERTIES + ['eri_tensor',
                                           'forces',
                                           'nuclear_forces',
                                           'electronic_forces']
    PARAM_SUPPORT = {'theory': ['rhf', 'rks', 'mp2'],
                     'functional': ['b3lyp', 'blyp', 'pbe0', 'x3lyp', 'MPW3LYP5']}

    FORCE_UNITS = u.hartree / u.bohr

    def __init__(self, **kwargs):
        super(PySCFPotential, self).__init__(**kwargs)
        self.pyscfmol = None
        self.reference = None
        self.kernel = None
        self.logs = StringIO()
        self.last_el_state = None
        self.logger = uibase.Logger('PySCF interface')

    @compute.runsremotely(enable=force_remote, is_imethod=True)
    def calculate(self, requests=None):
        self.logger = uibase.Logger('PySCF calc')
        do_forces = 'forces' in requests
        if do_forces and self.params.theory not in FORCE_CALCULATORS:
            raise ValueError('Forces are only available for the following theories:'
                             ','.join(FORCE_CALCULATORS))
        if do_forces:
            force_calculator = FORCE_CALCULATORS[self.params.theory]

        self.prep(force=True)  # rebuild every time

        # Set up initial guess
        if self.params.wfn_guess == 'stored':
            dm0 = self.params.initial_guess.density_matrix_a0
        else:
            dm0 = None

        # Compute reference WFN (if needed)
        refobj = self.pyscfmol
        if self.params.theory in NEEDS_REFERENCE:
            reference = self._build_theory(self.params.get('reference', 'rhf'),
                                           refobj)
            kernel, failures = self._converge(reference, dm0=dm0)
            refobj = self.reference = kernel
        else:
            self.reference = None

        # Compute WFN
        theory = self._build_theory(self.params['theory'],
                                    refobj)
        if self.params['theory'] not in IS_SCF:
            theory.kernel()
            self.kernel = theory
        else:
            self.kernel, failures = self._converge(theory, dm0=dm0)

        # Compute forces (if requested)
        if do_forces:
            grad = force_calculator(self.kernel)
        else:
            grad = None

        props = self._get_properties(self.reference, self.kernel, grad)

        if self.params.store_orb_guesses:
            self.params.wfn_guess = 'stored'
            self.params.initial_guess = props['wfn']

        return props

    def _get_properties(self, ref, kernel, grad):
        """ Analyze calculation results and return molecular properties

        Args:
            ref (pyscf.Kernel): Reference kernel (can be None)
            kernel (pyscf.Kernel): Theory kernel
            grad (pyscf.Gradient): Gradient calculation

        Returns:
            dict: Molecular property names and values
        """
        result = {}

        if self.reference is not None:
            result['reference_energy'] = (ref.e_tot*u.hartree).defunits()
            # TODO: check sign on correlation energy. Is this true for anything besides MP2?
            result['correlation_energy'] = (kernel.e_corr *u.hartree).defunits()
            result['potential_energy'] = result['correlation_energy'] + result['reference_energy']
            orb_calc = ref
        else:
            result['potential_energy'] = (kernel.e_tot*u.hartree).defunits()
            orb_calc = kernel

        if grad is not None:
            f_e = -1.0 * grad.grad_elec() * self.FORCE_UNITS
            f_n = -1.0 * grad.grad_nuc() * self.FORCE_UNITS
            result['electronic_forces'] = f_e.defunits()
            result['nuclear_forces'] = f_n.defunits()
            result['forces'] = result['electronic_forces'] + result['nuclear_forces']

        ao_matrices = self._get_ao_matrices(orb_calc)
        scf_matrices = self._get_scf_matrices(orb_calc, ao_matrices)
        ao_pop, atom_pop = orb_calc.mulliken_pop(verbose=-1)

        # Build the electronic state object
        basis = orbitals.basis.BasisSet(self.mol,
                                        orbitals=self._get_ao_basis_functions(),
                                        h1e=ao_matrices.h1e.defunits(),
                                        overlaps=scf_matrices.pop('sao'),
                                        name=self.params.basis)
        el_state = orbitals.wfn.ElectronicWfn(self.mol,
                                              self.pyscfmol.nelectron,
                                              aobasis=basis)

        # Build and store the canonical orbitals
        cmos = []
        for coeffs, energy, occ in zip(orb_calc.mo_coeff.T,
                                       orb_calc.mo_energy * u.hartree,
                                       orb_calc.get_occ()):
            cmos.append(orbitals.Orbital(coeffs, wfn=el_state, occupation=occ))
        el_state.add_orbitals(cmos, orbtype='canonical')

        # Return the result
        result['wfn'] = el_state
        self.last_el_state = el_state
        result['mulliken'] = DotDict({a: p for a, p in zip(self.mol.atoms, atom_pop)})
        result['mulliken'].type = 'atomic'
        return result

    def prep(self, force=False):
        # TODO: spin, isotopic mass, symmetry
        if self._prepped and not force: return
        self.pyscfmol = self._build_mol()
        self._prepped = True

    def _build_mol(self):
        """TODO: where does charge go? Model or molecule?"""
        pyscfmol = mol_to_pyscf(self.mol, self.params.basis,
                                symmetry=self.params.get('symmetry', None),
                                charge=self.get_formal_charge())
        pyscfmol.stdout = self.logs
        return pyscfmol

    def _converge(self, method, dm0=None):
        """
        Automatically try a bunch of fallback methods for convergence
        see also https://www.molpro.net/info/2015.1/doc/manual/node176.html#sec:difficulthf
        """
        # TODO: make this user configurable
        # TODO: generalize outside of pyscf

        energy = method.kernel(dm0=dm0)
        failed = []

        # stop here if it converged
        if method.converged:
            return method, failed

        # fallback 1: don't use previous density matrix OR change initial_guess
        failed.append(method)
        if dm0 is not None:
            method.init_guess = 'atom'
        else:
            method.init_guess = 'minao'
        self.logger.handled('SCF failed to converge. Retrying with initial guess %s' % method.init_guess)
        energy = method.kernel()
        if method.converged:
            return method, failed

        # fallback 2: level shift, slower convergence
        # this probably won't converge, but is intended to get us in the right basin for the next step
        # NEWFEATURE: should dynamically adjust level shift instead of hardcoded cycles
        self.logger.handled('SCF failed to converge. Performing %d iterations with level shift of -0.5 hartree'
                            % (method.max_cycle / 2))
        failed.append(method)
        method.init_guess = 'minao'
        method.level_shift = -0.5
        method.max_cycle /= 2
        energy = method.kernel()
        if method.converged:
            return method, failed

        # fallback 2 cont.: remove level shift and try to converge
        self.logger.handled('Removing level shift and continuing')
        level_shift_dm = method.make_rdm1()
        method.level_shift = 0.0
        method.max_cycle *= 2
        energy = method.kernel(dm0=level_shift_dm)
        if method.converged:
            return method, failed

        raise orbitals.ConvergenceError(method)

    def _build_theory(self, name, refobj):
        theory = THEORIES[name](refobj)

        theory.callback = StatusLogger('%s/%s procedure:' % (self.params.theory, self.params.basis),
                                       ['cycle', 'e_tot'],
                                       self.logger)

        if 'scf_cycles' in self.params:
            theory.max_cycle = self.params.scf_cycles

        if 'functional' in self.params:
            self._assign_functional(theory, name, self.params.get('functional', None))

        return theory

    def _assign_functional(self, kernel, theory, fname):
        if theory in NEEDS_FUNCTIONAL:
            if fname is not None:
                kernel.xc = fname
            else:
                raise ValueError('No functional specified for reference theory "%s"' % theory)
        #elif fname is not None:
            #raise ValueError('Functional specified for non-DFT theory "%s"' % theory)

    def _get_ao_basis_functions(self):
        """ Convert pyscf basis functions into a list of atomic basis functions

        Notes:
            PySCF stores *shells* instead of a flat list, so we need to do a little hacky
                guesswork to do this conversion. We include consistentcy checks with the annotated
                list of basis functions stored from ``mole.cart_labels()``
            As of PySCF v1.0, only cartesian orbitals appear to be supported, and that's all
                supported here right now

        Returns:
            List[moldesign.Gaussians.AtomicBasisFunction]

        """
        bfs = []
        pmol = self.pyscfmol

        orblabels = iter(pmol.spheric_labels())

        for ishell in xrange(pmol.nbas):  # loop over shells (n,l)
            atom = self.mol.atoms[pmol.bas_atom(ishell)]
            angular = pmol.bas_angular(ishell)
            num_momentum_states = angular*2 + 1
            exps = pmol.bas_exp(ishell)
            num_contractions = pmol.bas_nctr(ishell)
            coeffs = pmol.bas_ctr_coeff(ishell)

            for ictr in xrange(num_contractions):  # loop over contractions in shell
                for ibas in xrange(num_momentum_states):  # loop over angular states in shell
                    label = orblabels.next()
                    sphere_label = label[3]
                    l, m = SPHERICAL_NAMES[sphere_label]
                    assert l == angular
                    # TODO: This is not really the principal quantum number
                    n = int(''.join(x for x in label[2] if x.isdigit()))

                    primitives = [orbitals.SphericalGaussian(atom.position.copy(),
                                                       exp, n, l, m,
                                                       coeff=coeff[ictr])
                                  for exp, coeff in zip(exps, coeffs)]
                    bfs.append(orbitals.AtomicBasisFunction(atom, n=n, l=angular, m=m,
                                                      primitives=primitives))

        return bfs

    def _get_basis_name(self):
        """
        Translate basis_orbitals set name into a spec that pyscf recognizes
        :return:
        """
        # TODO: actually implement this
        return self.params.basis

    @staticmethod
    def _get_ao_matrices(mf):
        h1e = mf.get_hcore() * u.hartree
        sao = mf.get_ovlp()
        return DotDict(h1e=h1e, sao=sao)

    def _get_scf_matrices(self, mf, ao_mats):
        dm = mf.make_rdm1()
        veff = mf.get_veff(dm=dm) * u.hartree
        fock = ao_mats.h1e + veff
        scf_matrices = dict(density_matrix_ao=dm,
                            h2e=veff,
                            fock_ao=fock)
        scf_matrices.update(ao_mats)
        return scf_matrices