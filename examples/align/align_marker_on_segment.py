import biorbd

from bioptim import (
    Node,
    Axe,
    OptimalControlProgram,
    DynamicsList,
    DynamicsFcn,
    ObjectiveList,
    ObjectiveFcn,
    ConstraintList,
    ConstraintFcn,
    BoundsList,
    QAndQDotBounds,
    InitialGuessList,
    ShowResult,
    OdeSolver,
)


def prepare_ocp(
    biorbd_model_path,
    final_time,
    number_shooting_points,
    initialize_near_solution,
    ode_solver=OdeSolver.RK,
    constr=True,
    use_SX=False,
):
    # --- Options --- #
    # Model path
    biorbd_model = biorbd.Model(biorbd_model_path)

    # Problem parameters
    tau_min, tau_max, tau_init = -100, 100, 0

    # Add objective functions
    objective_functions = ObjectiveList()
    objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_TORQUE, weight=100)

    # Dynamics
    dynamics = DynamicsList()
    dynamics.add(DynamicsFcn.TORQUE_DRIVEN)

    # Constraints
    if constr is True:
        constraints = ConstraintList()
        constraints.add(ConstraintFcn.ALIGN_MARKERS, node=Node.START, first_marker_idx=0, second_marker_idx=4)
        constraints.add(ConstraintFcn.ALIGN_MARKERS, node=Node.END, first_marker_idx=0, second_marker_idx=5)
        constraints.add(
            ConstraintFcn.ALIGN_MARKER_WITH_SEGMENT_AXIS, node=Node.ALL, marker_idx=1, segment_idx=2, axis=(Axe.X)
        )
    else:
        constraints = ConstraintList()

    # Path constraint
    x_bounds = BoundsList()
    x_bounds.add(bounds=QAndQDotBounds(biorbd_model))

    for i in range(1, 8):
        if i != 3:
            x_bounds[0][i, [0, -1]] = 0
    x_bounds[0][2, -1] = 1.57

    # Initial guess
    x_init = InitialGuessList()
    x_init.add([0] * (biorbd_model.nbQ() + biorbd_model.nbQdot()))
    if initialize_near_solution:
        for i in range(2):
            x_init[0].init[i] = 1.5
        for i in range(4, 6):
            x_init[0].init[i] = 0.7
        for i in range(6, 8):
            x_init[0].init[i] = 0.6

    # Define control path constraint
    u_bounds = BoundsList()
    u_bounds.add([tau_min] * biorbd_model.nbGeneralizedTorque(), [tau_max] * biorbd_model.nbGeneralizedTorque())

    u_init = InitialGuessList()
    u_init.add([tau_init] * biorbd_model.nbGeneralizedTorque())

    # ------------- #

    return OptimalControlProgram(
        biorbd_model,
        dynamics,
        number_shooting_points,
        final_time,
        x_init,
        u_init,
        x_bounds,
        u_bounds,
        objective_functions,
        constraints,
        ode_solver=ode_solver,
        use_SX=use_SX,
    )


if __name__ == "__main__":
    ocp = prepare_ocp(
        biorbd_model_path="cube_and_line.bioMod",
        number_shooting_points=30,
        final_time=2,
        initialize_near_solution=True,
    )

    # --- Solve the program --- #
    sol = ocp.solve(show_online_optim=True)

    # --- Show results --- #
    result = ShowResult(ocp, sol)
    result.animate()
