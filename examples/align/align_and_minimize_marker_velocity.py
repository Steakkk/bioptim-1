import biorbd

from bioptim import (
    OptimalControlProgram,
    DynamicsList,
    DynamicsFcn,
    ObjectiveList,
    ObjectiveFcn,
    BoundsList,
    QAndQDotBounds,
    InitialGuessList,
    ShowResult,
    ControlType,
    OdeSolver,
)


def prepare_ocp(
    biorbd_model_path,
    final_time,
    number_shooting_points,
    marker_velocity_or_displacement,
    marker_in_first_coordinates_system,
    control_type,
    ode_solver=OdeSolver.RK,
):
    # --- Options --- #
    # Model path
    biorbd_model = biorbd.Model(biorbd_model_path)
    nq = biorbd_model.nbQ()
    biorbd_model.markerNames()
    # Problem parameters
    tau_min, tau_max, tau_init = -100, 100, 0

    # Add objective functions
    if marker_in_first_coordinates_system:
        # Marker should follow this segment (0 velocity when compare to this one)
        coordinates_system_idx = 0
    else:
        # Marker should be static in global reference frame
        coordinates_system_idx = -1

    objective_functions = ObjectiveList()
    if marker_velocity_or_displacement == "disp":
        objective_functions.add(
            ObjectiveFcn.Lagrange.MINIMIZE_MARKERS_DISPLACEMENT,
            coordinates_system_idx=coordinates_system_idx,
            index=6,
            weight=1000,
        )
    elif marker_velocity_or_displacement == "velo":
        objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_MARKERS_VELOCITY, index=6, weight=1000)
    else:
        raise RuntimeError(
            "Wrong choice of marker_velocity_or_displacement, actual value is "
            "{marker_velocity_or_displacement}, should be 'velo' or 'disp'."
        )
    # Make sure the segments actually moves (in order to test the relative speed objective)
    objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_STATE, index=6, weight=-1)
    objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_STATE, index=7, weight=-1)

    # Dynamics
    dynamics = DynamicsList()
    dynamics.add(DynamicsFcn.TORQUE_DRIVEN)

    # Path constraint
    x_bounds = BoundsList()
    x_bounds.add(bounds=QAndQDotBounds(biorbd_model))

    for i in range(nq, 2 * nq):
        x_bounds[0].min[i, :] = -10
        x_bounds[0].max[i, :] = 10

    # Initial guess
    x_init = InitialGuessList()
    x_init.add([1.5, 1.5, 0.0, 0.0, 0.7, 0.7, 0.6, 0.6])

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
        nb_integration_steps=5,
        control_type=control_type,
        ode_solver=ode_solver,
    )


if __name__ == "__main__":
    ocp = prepare_ocp(
        biorbd_model_path="cube_and_line.bioMod",
        number_shooting_points=30,
        final_time=2,
        marker_velocity_or_displacement="disp",  # "velo"
        marker_in_first_coordinates_system=True,
        control_type=ControlType.LINEAR_CONTINUOUS,
    )

    # --- Solve the program --- #
    sol = ocp.solve(show_online_optim=True)

    # --- Show results --- #
    result = ShowResult(ocp, sol)
    result.animate(nb_frames=200)
