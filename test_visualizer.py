import sys
import time
from pathlib import Path
from ktv_psa_scheduler.pipeline import run_pipeline
from ktv_psa_scheduler.model import build_tsn_graph, solve_model
from ktv_psa_scheduler.visualizer import plot_train_string_chart

print('Running pipeline...')
data_dir = Path('d:/A_Resume_Projects/Railway_Optimization/data')
t0 = time.time()
output = run_pipeline(data_dir=data_dir, window_start_minutes=0, horizon_size_minutes=120)
print(f'Pipeline done in {time.time()-t0:.2f}s. Loaded {len(output.freight_loads)} freight trains.')

print('Building TSN graph...')
t0 = time.time()
edges = build_tsn_graph(output, data_dir)
print(f'Graph built in {time.time()-t0:.2f}s. Generated {len(edges)} feasible edges.')

print('Solving model (time limit 30s)...')
t0 = time.time()
result = solve_model(output, edges, time_limit_seconds=30.0)

print(f'Solve status: {result.status} in {time.time()-t0:.2f}s')
print(f'Variables: {result.num_variables}, Constraints: {result.num_constraints}')
print(f'Optimal Objective: {result.objective_value}')

print('Generating plot...')
t0 = time.time()
plot_train_string_chart(output, result, save_path='chart.html')
print(f'Plot generated in {time.time()-t0:.2f}s.')
print('Done!')
