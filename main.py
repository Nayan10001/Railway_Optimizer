import argparse
from pathlib import Path
from ktv_psa_scheduler.orchestrator import run_rolling_horizon, OrchestratorConfig
from ktv_psa_scheduler.pipeline import run_pipeline
from ktv_psa_scheduler.visualizer import plot_orchestrator_string_chart


def main():
    parser = argparse.ArgumentParser(description="KTV-PSA Freight Scheduler Optimization Engine")
    parser.add_argument("--data-dir", type=str, default="data", help="Path to data directory")
    parser.add_argument("--plan-horizon", type=int, default=360, help="Plan horizon per window in minutes")
    parser.add_argument("--freeze-horizon", type=int, default=120, help="Freeze horizon per window in minutes")
    parser.add_argument("--total-horizon", type=int, default=1440, help="Total planning horizon in minutes")
    parser.add_argument("--time-limit", type=float, default=120.0, help="Solver time limit per window in seconds")
    parser.add_argument("--mip-gap", type=float, default=0.01, help="MIP gap tolerance (e.g. 0.01 for 1%)")
    parser.add_argument("--plot", action="store_true", help="Generate train string chart visualization")
    parser.add_argument("--output", type=str, default="orchestrator_chart.html", help="Output path for the chart")
    
    args = parser.parse_args()
    
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"Error: Data directory '{data_dir}' not found.")
        return
        
    config = OrchestratorConfig(
        plan_horizon_minutes=args.plan_horizon,
        freeze_horizon_minutes=args.freeze_horizon,
        total_horizon_minutes=args.total_horizon,
        solve_time_limit=args.time_limit,
        mip_gap=args.mip_gap
    )
    
    print(f"Starting Rolling-Horizon Scheduler...")
    print(f"Configuration: {config}")
    
    result = run_rolling_horizon(data_dir, config=config, verbose=True)
    
    if args.plot and result.frozen_slices:
        print(f"Loading master pipeline for visualization (horizon={args.total_horizon}m)...")
        # Extract the epoch_t0 from the first slice or pipeline run
        # Running the pipeline for the full horizon just to gather passenger entries
        try:
            master_output = run_pipeline(
                data_dir=data_dir,
                window_start_minutes=0,
                horizon_size_minutes=args.total_horizon
            )
            print(f"Generating visualization to {args.output}...")
            plot_orchestrator_string_chart(master_output, result, args.output)
        except Exception as e:
            print(f"Failed to generate visualization: {e}")

if __name__ == "__main__":
    main()
