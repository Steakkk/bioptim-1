import os
import pickle

from casadi import vertcat, sum1, nlpsol, SX, MX

from .solver_interface import SolverInterface
from ..gui.plot import OnlineCallback
from ..limits.path_conditions import Bounds
from ..misc.enums import InterpolationType


class IpoptInterface(SolverInterface):
    def __init__(self, ocp):
        super().__init__(ocp)

        self.options_common = {}
        self.opts = None

        self.lam_g = None
        self.lam_x = None

        self.ipopt_nlp = None
        self.ipopt_limits = None
        self.ocp_solver = None

        self.bobo_directory = ".__tmp_biorbd_optim"
        self.bobo_file_path = ".__tmp_biorbd_optim/temp_save_iter.bobo"

    def online_optim(self, ocp):
        self.options_common["iteration_callback"] = OnlineCallback(ocp)

    def start_get_iterations(self):
        if os.path.isfile(self.bobo_file_path):
            os.remove(self.bobo_file_path)
            os.rmdir(self.bobo_directory)
        os.mkdir(self.bobo_directory)

        with open(self.bobo_file_path, "wb") as file:
            pickle.dump([], file)

    def finish_get_iterations(self):
        with open(self.bobo_file_path, "rb") as file:
            self.out["sol_iterations"] = pickle.load(file)
            os.remove(self.bobo_file_path)
            os.rmdir(self.bobo_directory)

    def configure(self, solver_options):
        options = {
            "ipopt.tol": 1e-6,
            "ipopt.max_iter": 1000,
            "ipopt.hessian_approximation": "exact",  # "exact", "limited-memory"
            "ipopt.limited_memory_max_history": 50,
            "ipopt.linear_solver": "mumps",  # "ma57", "ma86", "mumps"
        }
        for key in solver_options:
            ipopt_key = key
            if key[:6] != "ipopt.":
                ipopt_key = "ipopt." + key
            options[ipopt_key] = solver_options[key]
        self.opts = {**options, **self.options_common}

    def solve(self):
        all_J = self.__dispatch_obj_func()
        all_g, all_g_bounds = self.__dispatch_bounds()

        self.ipopt_nlp = {"x": self.ocp.V, "f": sum1(all_J), "g": all_g}
        self.ipopt_limits = {
            "lbx": self.ocp.V_bounds.min,
            "ubx": self.ocp.V_bounds.max,
            "lbg": all_g_bounds.min,
            "ubg": all_g_bounds.max,
            "x0": self.ocp.V_init.init,
        }

        if self.lam_g is not None:
            self.ipopt_limits["lam_g0"] = self.lam_g
        if self.lam_x is not None:
            self.ipopt_limits["lam_x0"] = self.lam_x

        solver = nlpsol("nlpsol", "ipopt", self.ipopt_nlp, self.opts)

        # Solve the problem
        self.out = {"sol": solver.call(self.ipopt_limits)}
        self.out["sol"]["time_tot"] = solver.stats()["t_wall_total"]
        # To match acados convention (0 = success, 1 = error)
        self.out["sol"]["status"] = int(not solver.stats()["success"])

        return self.out

    def set_lagrange_multiplier(self, sol):
        self.lam_g = sol["lam_g"]
        self.lam_x = sol["lam_x"]

    def __dispatch_bounds(self):
        all_g = self.ocp.CX()
        all_g_bounds = Bounds(interpolation=InterpolationType.CONSTANT)
        for i in range(len(self.ocp.g)):
            for j in range(len(self.ocp.g[i])):
                all_g = vertcat(all_g, self.ocp.g[i][j]["val"])
                all_g_bounds.concatenate(self.ocp.g[i][j]["bounds"])
        for nlp in self.ocp.nlp:
            for i in range(len(nlp.g)):
                for j in range(len(nlp.g[i])):
                    all_g = vertcat(all_g, nlp.g[i][j]["val"])
                    all_g_bounds.concatenate(nlp.g[i][j]["bounds"])

        if isinstance(all_g_bounds.min, (SX, MX)) or isinstance(all_g_bounds.max, (SX, MX)):
            raise RuntimeError("Ipopt doesn't support SX/MX types in constraints bounds")
        return all_g, all_g_bounds

    def __dispatch_obj_func(self):
        all_J = self.ocp.CX()
        for j_nodes in self.ocp.J:
            for obj in j_nodes:
                all_J = vertcat(all_J, IpoptInterface.finalize_objective_value(obj))
        for nlp in self.ocp.nlp:
            for obj_nodes in nlp.J:
                for obj in obj_nodes:
                    all_J = vertcat(all_J, IpoptInterface.finalize_objective_value(obj))

        return all_J
