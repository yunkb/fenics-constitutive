"""
Helper classes and functions
============================

Suppress warnings
-----------------

The whole quadrature space is half deprecated, half not. We roll with it 
and just ignore the warnings.
"""

import numpy as np
import warnings

from dolfin import *
from ffc.quadrature.deprecation import QuadratureRepresentationDeprecationWarning

parameters["form_compiler"]["representation"] = "quadrature"
warnings.simplefilter("ignore", QuadratureRepresentationDeprecationWarning)


try:
    from fenics_helpers.boundary import *
    from fenics_helpers.timestepping import TimeStepper
except Exception as e:
    print("Install fenics_helpers via (e.g.)")
    print("   pip3 install git+https://github.com/BAMResearch/fenics_helpers")
    raise (e)


"""
Load-displacement curve
-----------------------

For a given state (e.g. a time step), load-displacement curve connects the 
displacements of certain DOFs with their reaction forces (*load*). Especially
for strain-softening materials, this curve can indicate numerical issues
like snap-backs.

From ``dolfin.DirichletBC.get_boundary_values()`` , we directly extract the 
``.values()`` as the current displacements and the ``.keys()`` as the DOF 
numbers. The latter ones are used to extract the reaction (out-of-balance) 
forces from a given force vector ``R``.

Special care is taken to make this class work in parallel.
"""


class LoadDisplacementCurve:
    def __init__(self, bc):
        """
        bc:
            dolfin.DirichletBC 
        """
        self.comm = MPI.comm_world
        self.bc = bc

        self.dofs = list(self.bc.get_boundary_values().keys())
        self.n_dofs = MPI.sum(self.comm, len(self.dofs))

        self.load = []
        self.disp = []
        self.ts = []
        self.plot = None

        self.is_root = MPI.rank(self.comm) == 0

    def __call__(self, t, R):
        """
        t:
            global time
        R:
            residual, out of balance forces
        """
        # A dof > R.local_size() is (I GUESS?!?!) a ghost node and its
        # contribution to R is accounted for on the owning process. So it
        # is (I GUESS?!) safe to ignore it.
        self.dofs = [d for d in self.dofs if d < R.local_size()]

        load_local = np.sum(R[self.dofs])
        load = MPI.sum(self.comm, load_local)

        disp_local = np.sum(list(self.bc.get_boundary_values().values()))
        disp = MPI.sum(self.comm, disp_local) / self.n_dofs

        self.load.append(load)
        self.disp.append(disp)
        self.ts.append(t)
        if self.plot and self.is_root:
            self.plot(disp, load)

    def show(self, fmt="-rx"):
        if self.is_root:
            from fenics_helpers.plotting import AdaptivePlot

            self.plot = AdaptivePlot(fmt)

    def keep(self):
        if self.is_root:
            self.plot.keep()

"""
Local projector
---------------

Projecting an expression ``expr(u)`` into a function space ``V`` is done by
solving the variational problem

.. math::
    \int_\Omega uv \ \mathrm dx = \int_\omega \text{expr} \ v \ \mathrm dx

for all test functions $v \in V$.

In our case, $V$ is a quadrature function space and can significantly speed
up this solution by using the ``dolfin.LocalSolver`` that can additionaly
be prefactorized to speedup subsequent projections.
"""


class LocalProjector:
    def __init__(self, expr, V, dxm):
        """
        expr:
            expression to project
        V:
            quadrature function space
        dxm:
            dolfin.Measure("dx") that matches V
        """
        dv = TrialFunction(V)
        v_ = TestFunction(V)
        a_proj = inner(dv, v_) * dxm
        b_proj = inner(expr, v_) * dxm
        self.solver = LocalSolver(a_proj, b_proj)
        self.solver.factorize()

    def __call__(self, u):
        """
        u:
            function that is filled with the solution of the projection
        """
        self.solver.solve_local_rhs(u)

"""
Setting values for the quadrature space
---------------------------------------

* The combination of ``.zero`` and ``.add_local`` is faster than using
  ``.set_local`` directly, as ``.set_local`` copies everything in a `C++ vector <https://bitbucket.org/fenics-project/dolfin/src/946dbd3e268dc20c64778eb5b734941ca5c343e5/python/src/la.cpp#lines-576>`__ first.
* ``.apply("insert")`` takes care of the ghost value communication in a 
  parallel run.

"""

def set_q(q, values):
    """
    q:
        quadrature function space
    values:
        entries for `q`
    """
    v = q.vector()
    v.zero()
    v.add_local(values.flat)
    v.apply("insert")

