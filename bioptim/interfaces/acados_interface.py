from datetime import datetime

import numpy as np
from scipy import linalg
from casadi import SX, vertcat, Function, MX
from acados_template import AcadosModel, AcadosOcp, AcadosOcpSolver

from ..misc.enums import Node
from .solver_interface import SolverInterface
from ..limits.objective_functions import ObjectiveFunction
from ..limits.path_conditions import Bounds
from ..misc.enums import InterpolationType
from ..limits.penalty import PenaltyType


class AcadosInterface(SolverInterface):
    def __init__(self, ocp, **solver_options):
        if not isinstance(ocp.CX(), SX):
            raise RuntimeError("CasADi graph must be SX to be solved with ACADOS. Please set use_SX to True in OCP")

        super().__init__(ocp)

        # If Acados is installed using the acados_install.sh file, you probably can leave this to unset
        acados_path = ""
        if "acados_dir" in solver_options:
            acados_path = solver_options["acados_dir"]
        self.acados_ocp = AcadosOcp(acados_path=acados_path)
        self.acados_model = AcadosModel()

        if "cost_type" in solver_options:
            self.__set_cost_type(solver_options["cost_type"])
        else:
            self.__set_cost_type()

        if "constr_type" in solver_options:
            self.__set_constr_type(solver_options["constr_type"])
        else:
            self.__set_constr_type()

        self.lagrange_costs = SX()
        self.mayer_costs = SX()
        self.y_ref = []
        self.y_ref_end = []
        self.__acados_export_model(ocp)
        self.__prepare_acados(ocp)
        self.ocp_solver = None
        self.W = np.zeros((0, 0))
        self.W_e = np.zeros((0, 0))
        self.status = None
        self.out = {}

    def __acados_export_model(self, ocp):
        if ocp.nb_phases > 1:
            raise NotImplementedError("More than 1 phase is not implemented yet with ACADOS backend")

        # Declare model variables
        x = ocp.nlp[0].X[0]
        u = ocp.nlp[0].U[0]
        p = ocp.nlp[0].p
        if ocp.nlp[0].parameters_to_optimize:
            for n in range(ocp.nb_phases):
                for i in range(len(ocp.nlp[0].parameters_to_optimize)):
                    if str(ocp.nlp[0].p[i]) == f"time_phase_{n}":
                        raise RuntimeError("Time constraint not implemented yet with Acados.")

        self.params = ocp.nlp[0].parameters_to_optimize
        x = vertcat(p, x)
        x_dot = SX.sym("x_dot", x.shape[0], x.shape[1])

        f_expl = vertcat([0] * ocp.nlp[0].np, ocp.nlp[0].dynamics_func(x[ocp.nlp[0].np :, :], u, p))
        f_impl = x_dot - f_expl

        self.acados_model.f_impl_expr = f_impl
        self.acados_model.f_expl_expr = f_expl
        self.acados_model.x = x
        self.acados_model.xdot = x_dot
        self.acados_model.u = u
        self.acados_model.con_h_expr = np.zeros((0, 0))
        self.acados_model.con_h_expr_e = np.zeros((0, 0))
        self.acados_model.p = []
        now = datetime.now()  # current date and time
        self.acados_model.name = f"model_{now.strftime('%Y_%m_%d_%H%M%S%f')[:-4]}"

    def __prepare_acados(self, ocp):
        # set model
        self.acados_ocp.model = self.acados_model

        # set time
        self.acados_ocp.solver_options.tf = ocp.nlp[0].tf

        # set dimensions
        self.acados_ocp.dims.nx = ocp.nlp[0].nx + ocp.nlp[0].np
        self.acados_ocp.dims.nu = ocp.nlp[0].nu
        self.acados_ocp.dims.N = ocp.nlp[0].ns

    def __set_constr_type(self, constr_type="BGH"):
        self.acados_ocp.constraints.constr_type = constr_type
        self.acados_ocp.constraints.constr_type_e = constr_type

    def __set_constrs(self, ocp):
        # constraints handling in self.acados_ocp
        u_min = np.array(ocp.nlp[0].u_bounds.min)
        u_max = np.array(ocp.nlp[0].u_bounds.max)
        x_min = np.array(ocp.nlp[0].x_bounds.min)
        x_max = np.array(ocp.nlp[0].x_bounds.max)
        self.all_constr = SX()
        self.end_constr = SX()
        ##TODO:change for more node flexibility on bounds
        self.all_g_bounds = Bounds(interpolation=InterpolationType.CONSTANT)
        self.end_g_bounds = Bounds(interpolation=InterpolationType.CONSTANT)
        for i in range(ocp.nb_phases):
            for g, G in enumerate(ocp.nlp[i].g):
                if not G:
                    continue
                if G[0]["constraint"].node[0] is Node.ALL:
                    self.all_constr = vertcat(self.all_constr, G[0]["val"].reshape((-1, 1)))
                    self.all_g_bounds.concatenate(G[0]["bounds"])
                    if len(G) > ocp.nlp[0].ns:
                        constr_end_func_tp = Function(f"cas_constr_end_func_{i}_{g}", [ocp.nlp[i].X[-1]], [G[0]["val"]])
                        self.end_constr = vertcat(self.end_constr, constr_end_func_tp(ocp.nlp[i].X[0]).reshape((-1, 1)))
                        self.end_g_bounds.concatenate(G[0]["bounds"])

                elif G[0]["constraint"].node[0] is Node.END:
                    constr_end_func_tp = Function(f"cas_constr_end_func_{i}_{g}", [ocp.nlp[i].X[-1]], [G[0]["val"]])
                    self.end_constr = vertcat(self.end_constr, constr_end_func_tp(ocp.nlp[i].X[0]).reshape((-1, 1)))
                    self.end_g_bounds.concatenate(G[0]["bounds"])

                else:
                    raise RuntimeError(
                        "Except for states and controls, Acados solver only handles constraints on last or all nodes."
                    )

        self.acados_model.con_h_expr = self.all_constr
        self.acados_model.con_h_expr_e = self.end_constr

        if not np.all(np.all(u_min.T == u_min.T[0, :], axis=0)):
            raise NotImplementedError("u_bounds min must be the same at each shooting point with ACADOS")
        if not np.all(np.all(u_max.T == u_max.T[0, :], axis=0)):
            raise NotImplementedError("u_bounds max must be the same at each shooting point with ACADOS")

        if (
            not np.isfinite(u_min).all()
            or not np.isfinite(x_min).all()
            or not np.isfinite(u_max).all()
            or not np.isfinite(x_max).all()
        ):
            raise NotImplementedError(
                "u_bounds and x_bounds cannot be set to infinity in ACADOS. Consider changing it"
                "to a big value instead."
            )

        # setup state constraints
        self.x_bound_max = np.ndarray((self.acados_ocp.dims.nx, 3))
        self.x_bound_min = np.ndarray((self.acados_ocp.dims.nx, 3))
        param_bounds_max = []
        param_bounds_min = []

        if self.params:
            param_bounds_max = np.concatenate([self.params[key].bounds.max for key in self.params.keys()])[:, 0]
            param_bounds_min = np.concatenate([self.params[key].bounds.min for key in self.params.keys()])[:, 0]

        for i in range(3):
            self.x_bound_max[:, i] = np.concatenate((param_bounds_max, np.array(ocp.nlp[0].x_bounds.max[:, i])))
            self.x_bound_min[:, i] = np.concatenate((param_bounds_min, np.array(ocp.nlp[0].x_bounds.min[:, i])))

        # setup control constraints
        self.acados_ocp.constraints.lbu = np.array(ocp.nlp[0].u_bounds.min[:, 0])
        self.acados_ocp.constraints.ubu = np.array(ocp.nlp[0].u_bounds.max[:, 0])
        self.acados_ocp.constraints.idxbu = np.array(range(self.acados_ocp.dims.nu))
        self.acados_ocp.dims.nbu = self.acados_ocp.dims.nu

        # initial state constraints
        self.acados_ocp.constraints.lbx_0 = self.x_bound_min[:, 0]
        self.acados_ocp.constraints.ubx_0 = self.x_bound_max[:, 0]
        self.acados_ocp.constraints.idxbx_0 = np.array(range(self.acados_ocp.dims.nx))
        self.acados_ocp.dims.nbx_0 = self.acados_ocp.dims.nx

        # setup path state constraints
        self.acados_ocp.constraints.Jbx = np.eye(self.acados_ocp.dims.nx)
        self.acados_ocp.constraints.lbx = self.x_bound_min[:, 1]
        self.acados_ocp.constraints.ubx = self.x_bound_max[:, 1]
        self.acados_ocp.constraints.idxbx = np.array(range(self.acados_ocp.dims.nx))
        self.acados_ocp.dims.nbx = self.acados_ocp.dims.nx

        # setup terminal state constraints
        self.acados_ocp.constraints.Jbx_e = np.eye(self.acados_ocp.dims.nx)
        self.acados_ocp.constraints.lbx_e = self.x_bound_min[:, -1]
        self.acados_ocp.constraints.ubx_e = self.x_bound_max[:, -1]
        self.acados_ocp.constraints.idxbx_e = np.array(range(self.acados_ocp.dims.nx))
        self.acados_ocp.dims.nbx_e = self.acados_ocp.dims.nx

        # setup algebraic constraint
        self.acados_ocp.constraints.lh = np.array(self.all_g_bounds.min[:, 0])
        self.acados_ocp.constraints.uh = np.array(self.all_g_bounds.max[:, 0])

        # setup terminal algebraic constraint
        self.acados_ocp.constraints.lh_e = np.array(self.end_g_bounds.min[:, 0])
        self.acados_ocp.constraints.uh_e = np.array(self.end_g_bounds.max[:, 0])

    def __set_cost_type(self, cost_type="NONLINEAR_LS"):
        self.acados_ocp.cost.cost_type = cost_type
        self.acados_ocp.cost.cost_type_e = cost_type

    def __set_costs(self, ocp):

        if ocp.nb_phases != 1:
            raise NotImplementedError("ACADOS with more than one phase is not implemented yet.")
        # costs handling in self.acados_ocp
        self.y_ref = []
        self.y_ref_end = []
        self.lagrange_costs = SX()
        self.mayer_costs = SX()
        self.W = np.zeros((0, 0))
        self.W_e = np.zeros((0, 0))
        ctrl_objs = [
            PenaltyType.MINIMIZE_TORQUE,
            PenaltyType.MINIMIZE_MUSCLES_CONTROL,
            PenaltyType.MINIMIZE_ALL_CONTROLS,
        ]
        state_objs = [
            PenaltyType.MINIMIZE_STATE,
        ]

        if self.acados_ocp.cost.cost_type == "LINEAR_LS":
            self.Vu = np.array([], dtype=np.int64).reshape(0, ocp.nlp[0].nu)
            self.Vx = np.array([], dtype=np.int64).reshape(0, ocp.nlp[0].nx)
            self.Vxe = np.array([], dtype=np.int64).reshape(0, ocp.nlp[0].nx)
            for i in range(ocp.nb_phases):
                for j, J in enumerate(ocp.nlp[i].J):
                    if J[0]["objective"].type.get_type() == ObjectiveFunction.LagrangeFunction:
                        if J[0]["objective"].type.value[0] in ctrl_objs:
                            index = (
                                J[0]["objective"].index if J[0]["objective"].index else list(np.arange(ocp.nlp[0].nu))
                            )
                            vu = np.zeros(ocp.nlp[0].nu)
                            vu[index] = 1.0
                            self.Vu = np.vstack((self.Vu, np.diag(vu)))
                            self.Vx = np.vstack((self.Vx, np.zeros((ocp.nlp[0].nu, ocp.nlp[0].nx))))
                            self.W = linalg.block_diag(self.W, np.diag([J[0]["objective"].weight] * ocp.nlp[0].nu))
                            if J[0]["target"] is not None:
                                y_tp = np.zeros((ocp.nlp[0].nu, 1))
                                y_ref_tp = []
                                for J_tp in J:
                                    y_tp[index] = J_tp["target"].T.reshape((-1, 1))
                                    y_ref_tp.append(y_tp)
                                self.y_ref.append(y_ref_tp)
                            else:
                                self.y_ref.append([np.zeros((ocp.nlp[0].nu, 1)) for J_tp in J])
                        elif J[0]["objective"].type.value[0] in state_objs:
                            index = (
                                J[0]["objective"].index if J[0]["objective"].index else list(np.arange(ocp.nlp[0].nx))
                            )
                            vx = np.zeros(ocp.nlp[0].nx)
                            vx[index] = 1.0
                            self.Vx = np.vstack((self.Vx, np.diag(vx)))
                            self.Vu = np.vstack((self.Vu, np.zeros((ocp.nlp[0].nx, ocp.nlp[0].nu))))
                            self.W = linalg.block_diag(self.W, np.diag([J[0]["objective"].weight] * ocp.nlp[0].nx))
                            if J[0]["target"] is not None:
                                y_ref_tp = []
                                for J_tp in J:
                                    y_tp = np.zeros((ocp.nlp[0].nx, 1))
                                    y_tp[index] = J_tp["target"].T.reshape((-1, 1))
                                    y_ref_tp.append(y_tp)
                                self.y_ref.append(y_ref_tp)
                            else:
                                self.y_ref.append([np.zeros((ocp.nlp[0].nx, 1)) for J_tp in J])
                        else:
                            raise RuntimeError(
                                f"{J[0]['objective'].type.name} is an incompatible objective term with "
                                f"LINEAR_LS cost type"
                            )

                        # Deal with last node to match ipopt formulation
                        if J[0]["objective"].node[0].value == "all" and len(J) > ocp.nlp[0].ns:
                            index = (
                                J[0]["objective"].index if J[0]["objective"].index else list(np.arange(ocp.nlp[0].nx))
                            )
                            vxe = np.zeros(ocp.nlp[0].nx)
                            vxe[index] = 1.0
                            self.Vxe = np.vstack((self.Vxe, np.diag(vxe)))
                            self.W_e = linalg.block_diag(self.W_e, np.diag([J[0]["objective"].weight] * ocp.nlp[0].nx))
                            if J[0]["target"] is not None:
                                y_tp = np.zeros((ocp.nlp[0].nx, 1))
                                y_tp[index] = J[-1]["target"].T.reshape((-1, 1))
                                self.y_ref_end.append(y_tp)
                            else:
                                self.y_ref_end.append(np.zeros((ocp.nlp[0].nx, 1)))

                    elif J[0]["objective"].type.get_type() == ObjectiveFunction.MayerFunction:
                        if J[0]["objective"].type.value[0] in state_objs:
                            index = (
                                J[0]["objective"].index if J[0]["objective"].index else list(np.arange(ocp.nlp[0].nx))
                            )
                            vxe = np.zeros(ocp.nlp[0].nx)
                            vxe[index] = 1.0
                            self.Vxe = np.vstack((self.Vxe, np.diag(vxe)))
                            self.W_e = linalg.block_diag(self.W_e, np.diag([J[0]["objective"].weight] * ocp.nlp[0].nx))
                            if J[0]["target"] is not None:
                                y_tp = np.zeros((ocp.nlp[0].nx, 1))
                                y_tp[index] = J[-1]["target"].T.reshape((-1, 1))
                                self.y_ref_end.append(y_tp)
                            else:
                                self.y_ref_end.append(np.zeros((ocp.nlp[0].nx, 1)))
                        else:
                            raise RuntimeError(
                                f"{J[0]['objective'].type.name} is an incompatible objective term "
                                f"with LINEAR_LS cost type"
                            )

                    else:
                        raise RuntimeError("The objective function is not Lagrange nor Mayer.")

                if self.params:
                    raise RuntimeError("Params not yet handled with LINEAR_LS cost type")

            # Set costs
            self.acados_ocp.cost.Vx = self.Vx if self.Vx.shape[0] else np.zeros((0, 0))
            self.acados_ocp.cost.Vu = self.Vu if self.Vu.shape[0] else np.zeros((0, 0))
            self.acados_ocp.cost.Vx_e = self.Vxe if self.Vxe.shape[0] else np.zeros((0, 0))

            # Set dimensions
            self.acados_ocp.dims.ny = sum([len(data[0]) for data in self.y_ref])
            self.acados_ocp.dims.ny_e = sum([len(data) for data in self.y_ref_end])

            # Set weight
            self.acados_ocp.cost.W = self.W
            self.acados_ocp.cost.W_e = self.W_e

            # Set target shape
            self.acados_ocp.cost.yref = np.zeros((self.acados_ocp.cost.W.shape[0],))
            self.acados_ocp.cost.yref_e = np.zeros((self.acados_ocp.cost.W_e.shape[0],))

        elif self.acados_ocp.cost.cost_type == "NONLINEAR_LS":
            for i in range(ocp.nb_phases):
                for j, J in enumerate(ocp.nlp[i].J):
                    if J[0]["objective"].type.get_type() == ObjectiveFunction.LagrangeFunction:
                        self.lagrange_costs = vertcat(self.lagrange_costs, J[0]["val"].reshape((-1, 1)))
                        self.W = linalg.block_diag(self.W, np.diag([J[0]["objective"].weight] * J[0]["val"].numel()))
                        if J[0]["target"] is not None:
                            self.y_ref.append([J_tp["target"].T.reshape((-1, 1)) for J_tp in J])
                        else:
                            self.y_ref.append([np.zeros((J_tp["val"].numel(), 1)) for J_tp in J])

                        # Deal with last node to match ipopt formulation
                        if J[0]["objective"].node[0].value == "all" and len(J) > ocp.nlp[0].ns:
                            mayer_func_tp = Function(f"cas_mayer_func_{i}_{j}", [ocp.nlp[i].X[-1]], [J[0]["val"]])
                            self.W_e = linalg.block_diag(
                                self.W_e, np.diag([J[0]["objective"].weight] * J[0]["val"].numel())
                            )
                            self.mayer_costs = vertcat(
                                self.mayer_costs, mayer_func_tp(ocp.nlp[i].X[0]).reshape((-1, 1))
                            )
                            if J[0]["target"] is not None:
                                self.y_ref_end.append(J[-1]["target"].T.reshape((-1, 1)))
                            else:
                                self.y_ref_end.append(np.zeros((J[-1]["val"].numel(), 1)))

                    elif J[0]["objective"].type.get_type() == ObjectiveFunction.MayerFunction:
                        mayer_func_tp = Function(f"cas_mayer_func_{i}_{j}", [ocp.nlp[i].X[-1]], [J[0]["val"]])
                        self.W_e = linalg.block_diag(
                            self.W_e, np.diag([J[0]["objective"].weight] * J[0]["val"].numel())
                        )
                        self.mayer_costs = vertcat(self.mayer_costs, mayer_func_tp(ocp.nlp[i].X[0]).reshape((-1, 1)))
                        if J[0]["target"] is not None:
                            self.y_ref_end.append(J[0]["target"].T.reshape((-1, 1)))
                        else:
                            self.y_ref_end.append(np.zeros((J[0]["val"].numel(), 1)))

                    else:
                        raise RuntimeError("The objective function is not Lagrange nor Mayer.")

                # parameter as mayer function
                # IMPORTANT: it is considered that only parameters are stored in ocp.J, for now.
                if self.params:
                    for j, J in enumerate(ocp.J):
                        mayer_func_tp = Function(f"cas_J_mayer_func_{i}_{j}", [ocp.nlp[i].X[-1]], [J[0]["val"]])
                        self.W_e = linalg.block_diag(
                            self.W_e, np.diag(([J[0]["objective"].weight] * J[0]["val"].numel()))
                        )
                        self.mayer_costs = vertcat(self.mayer_costs, mayer_func_tp(ocp.nlp[i].X[0]).reshape((-1, 1)))
                        if J[0]["target"] is not None:
                            self.y_ref_end.append(J[0]["target"].T.reshape((-1, 1)))
                        else:
                            self.y_ref_end.append(np.zeros((J[0]["val"].numel(), 1)))

            # Set costs
            self.acados_ocp.model.cost_y_expr = self.lagrange_costs if self.lagrange_costs.numel() else SX(1, 1)
            self.acados_ocp.model.cost_y_expr_e = self.mayer_costs if self.mayer_costs.numel() else SX(1, 1)

            # Set dimensions
            self.acados_ocp.dims.ny = self.acados_ocp.model.cost_y_expr.shape[0]
            self.acados_ocp.dims.ny_e = self.acados_ocp.model.cost_y_expr_e.shape[0]

            # Set weight
            self.acados_ocp.cost.W = np.zeros((1, 1)) if self.W.shape == (0, 0) else self.W
            self.acados_ocp.cost.W_e = np.zeros((1, 1)) if self.W_e.shape == (0, 0) else self.W_e

            # Set target shape
            self.acados_ocp.cost.yref = np.zeros((self.acados_ocp.cost.W.shape[0],))
            self.acados_ocp.cost.yref_e = np.zeros((self.acados_ocp.cost.W_e.shape[0],))

        elif self.acados_ocp.cost.cost_type == "EXTERNAL":
            raise RuntimeError("EXTERNAL is not interfaced yet, please use NONLINEAR_LS")

        else:
            raise RuntimeError("Available acados cost type: 'LINEAR_LS', 'NONLINEAR_LS' and 'EXTERNAL'.")

    def __update_solver(self):
        param_init = []
        for n in range(self.acados_ocp.dims.N):
            if self.y_ref:
                self.ocp_solver.cost_set(n, "yref", np.vstack([data[n] for data in self.y_ref])[:, 0])
            # check following line
            # self.ocp_solver.cost_set(n, "W", self.W)

            if self.params:
                param_init = np.concatenate(
                    [self.params[key].initial_guess.init.evaluate_at(n) for key in self.params.keys()]
                )

            self.ocp_solver.set(n, "x", np.concatenate((param_init, self.ocp.nlp[0].x_init.init.evaluate_at(n))))
            self.ocp_solver.set(n, "u", self.ocp.nlp[0].u_init.init.evaluate_at(n))
            self.ocp_solver.constraints_set(n, "lbu", self.ocp.nlp[0].u_bounds.min[:, 0])
            self.ocp_solver.constraints_set(n, "ubu", self.ocp.nlp[0].u_bounds.max[:, 0])
            self.ocp_solver.constraints_set(n, "uh", self.all_g_bounds.max[:, 0])
            self.ocp_solver.constraints_set(n, "lh", self.all_g_bounds.min[:, 0])

            if n == 0:
                self.ocp_solver.constraints_set(n, "lbx", self.x_bound_min[:, 0])
                self.ocp_solver.constraints_set(n, "ubx", self.x_bound_max[:, 0])
            else:
                self.ocp_solver.constraints_set(n, "lbx", self.x_bound_min[:, 1])
                self.ocp_solver.constraints_set(n, "ubx", self.x_bound_max[:, 1])

        if self.y_ref_end:
            if len(self.y_ref_end) == 1:
                self.ocp_solver.cost_set(self.acados_ocp.dims.N, "yref", np.array(self.y_ref_end[0])[:, 0])
            else:
                self.ocp_solver.cost_set(
                    self.acados_ocp.dims.N, "yref", np.concatenate([data for data in self.y_ref_end])[:, 0]
                )
            # check following line
            # self.ocp_solver.cost_set(self.acados_ocp.dims.N, "W", self.W_e)
        self.ocp_solver.constraints_set(self.acados_ocp.dims.N, "lbx", self.x_bound_min[:, -1])
        self.ocp_solver.constraints_set(self.acados_ocp.dims.N, "ubx", self.x_bound_max[:, -1])
        if len(self.end_g_bounds.max[:, 0]):
            self.ocp_solver.constraints_set(self.acados_ocp.dims.N, "uh", self.end_g_bounds.max[:, 0])
            self.ocp_solver.constraints_set(self.acados_ocp.dims.N, "lh", self.end_g_bounds.min[:, 0])

        if self.ocp.nlp[0].x_init.init.shape[1] == self.acados_ocp.dims.N + 1:
            if self.params:
                self.ocp_solver.set(
                    self.acados_ocp.dims.N,
                    "x",
                    np.concatenate(
                        (
                            np.concatenate([self.params[key]["initial_guess"].init for key in self.params.keys()])[
                                :, 0
                            ],
                            self.ocp.nlp[0].x_init.init[:, self.acados_ocp.dims.N],
                        )
                    ),
                )
            else:
                self.ocp_solver.set(self.acados_ocp.dims.N, "x", self.ocp.nlp[0].x_init.init[:, self.acados_ocp.dims.N])

    def configure(self, options):
        if "acados_dir" in options:
            del options["acados_dir"]
        if "cost_type" in options:
            del options["cost_type"]
        if self.ocp_solver is None:
            self.acados_ocp.solver_options.qp_solver = "PARTIAL_CONDENSING_HPIPM"  # FULL_CONDENSING_QPOASES
            self.acados_ocp.solver_options.hessian_approx = "GAUSS_NEWTON"
            self.acados_ocp.solver_options.integrator_type = "IRK"
            self.acados_ocp.solver_options.nlp_solver_type = "SQP"

            self.acados_ocp.solver_options.nlp_solver_tol_comp = 1e-06
            self.acados_ocp.solver_options.nlp_solver_tol_eq = 1e-06
            self.acados_ocp.solver_options.nlp_solver_tol_ineq = 1e-06
            self.acados_ocp.solver_options.nlp_solver_tol_stat = 1e-06
            self.acados_ocp.solver_options.nlp_solver_max_iter = 200
            self.acados_ocp.solver_options.sim_method_newton_iter = 5
            self.acados_ocp.solver_options.sim_method_num_stages = 4
            self.acados_ocp.solver_options.sim_method_num_steps = 1
            self.acados_ocp.solver_options.print_level = 1
            for key in options:
                setattr(self.acados_ocp.solver_options, key, options[key])
        else:
            available_options = [
                "nlp_solver_tol_comp",
                "nlp_solver_tol_eq",
                "nlp_solver_tol_ineq",
                "nlp_solver_tol_stat",
            ]
            for key in options:
                if key in available_options:
                    short_key = key[11:]
                    self.ocp_solver.options_set(short_key, options[key])
                else:
                    raise RuntimeError(
                        f"[ACADOS] Only editable solver options after solver creation are :\n {available_options}"
                    )

    def get_iterations(self):
        raise NotImplementedError("return_iterations is not implemented yet with ACADOS backend")

    def online_optim(self, ocp):
        raise NotImplementedError("online_optim is not implemented yet with ACADOS backend")

    def get_optimized_value(self):
        ns = self.acados_ocp.dims.N
        nx = self.acados_ocp.dims.nx
        nq = self.ocp.nlp[0].q.shape[0]
        nparams = self.ocp.nlp[0].np
        acados_x = np.array([self.ocp_solver.get(i, "x") for i in range(ns + 1)]).T
        acados_p = acados_x[:nparams, :]
        acados_q = acados_x[nparams : nq + nparams, :]
        acados_qdot = acados_x[nq + nparams : nx, :]
        acados_u = np.array([self.ocp_solver.get(i, "u") for i in range(ns)]).T

        out = {
            "qqdot": acados_x,
            "x": [],
            "u": acados_u,
            "time_tot": self.ocp_solver.get_stats("time_tot")[0],
            "iter": self.ocp_solver.get_stats("sqp_iter")[0],
            "status": self.status,
        }
        for i in range(ns):
            out["x"] = vertcat(out["x"], acados_q[:, i])
            out["x"] = vertcat(out["x"], acados_qdot[:, i])
            out["x"] = vertcat(out["x"], acados_u[:, i])

        out["x"] = vertcat(out["x"], acados_q[:, ns])
        out["x"] = vertcat(out["x"], acados_qdot[:, ns])
        out["x"] = vertcat(acados_p[:, 0], out["x"])
        self.out["sol"] = out
        out = []
        for key in self.out.keys():
            out.append(self.out[key])
        return out[0] if len(out) == 1 else out

    def solve(self):
        # Populate costs and constrs vectors
        self.__set_costs(self.ocp)
        self.__set_constrs(self.ocp)
        if self.ocp_solver is None:
            self.ocp_solver = AcadosOcpSolver(self.acados_ocp, json_file="acados_ocp.json")
        self.__update_solver()
        self.status = self.ocp_solver.solve()
        self.get_optimized_value()
        return self
