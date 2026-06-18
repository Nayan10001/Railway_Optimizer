import streamlit as st
import time
from pathlib import Path

from ktv_psa_scheduler.orchestrator import run_rolling_horizon, OrchestratorConfig
from ktv_psa_scheduler.pipeline import run_pipeline
from ktv_psa_scheduler.visualizer import plot_orchestrator_string_chart

st.set_page_config(page_title="KTV-PSA Freight Scheduler", layout="wide")

st.title("🚆 KTV-PSA Freight Scheduler Engine")
st.markdown("Optimization dashboard for the Kottavalasa-Palasa corridor.")

# Sidebar configuration
st.sidebar.header("Orchestrator Settings")
plan_horizon = st.sidebar.number_input("Plan Horizon (min)", value=360, step=60)
freeze_horizon = st.sidebar.number_input("Freeze Horizon (min)", value=120, step=60)
total_horizon = st.sidebar.number_input("Total Horizon (min)", value=720, step=60)
time_limit = st.sidebar.number_input("Solver Time Limit (sec)", value=30.0, step=10.0)
mip_gap = st.sidebar.number_input("MIP Gap Target", value=0.01, format="%.3f")

data_dir_input = st.sidebar.text_input("Data Directory", value="data")

if st.sidebar.button("Run Scheduler"):
    data_dir = Path(data_dir_input)
    if not data_dir.exists():
        st.error(f"Data directory '{data_dir}' not found.")
    else:
        config = OrchestratorConfig(
            plan_horizon_minutes=int(plan_horizon),
            freeze_horizon_minutes=int(freeze_horizon),
            total_horizon_minutes=int(total_horizon),
            solve_time_limit=float(time_limit),
            mip_gap=float(mip_gap)
        )
        
        with st.spinner(f"Running rolling-horizon scheduler for {total_horizon} minutes..."):
            t_start = time.time()
            result = run_rolling_horizon(data_dir, config=config, verbose=False)
            t_end = time.time()
            
        st.success(f"Scheduler finished in {t_end - t_start:.2f} seconds!")
        
        # Display high-level metrics
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Windows Solved", result.windows_solved)
        col2.metric("Trains Scheduled", result.total_trains_scheduled)
        col3.metric("Edges Committed", result.total_edges_committed)
        col4.metric("Total Solve Time", f"{result.total_solve_time:.2f} s")
        
        # Plot
        with st.spinner("Generating Méridien Diagram..."):
            try:
                master_output = run_pipeline(
                    data_dir=data_dir,
                    window_start_minutes=0,
                    horizon_size_minutes=int(total_horizon)
                )
                fig = plot_orchestrator_string_chart(master_output, result, save_path="orchestrator_chart.html")
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.error(f"Failed to generate visualization: {e}")
