import subprocess
import time
import sys

def main():
    run_id = f"demo-run-{int(time.time())}"
    print(f"Iniciando demostración con run_id: {run_id}")
    
    # 1. Arrancar el run de iworkflow en segundo plano
    proc_run = subprocess.Popen([
        sys.executable, "-m", "iworkflow.cli", "run",
        "--spec", "examples/demo_dashboard.json",
        "--run-id", run_id
    ])

    # Esperar 1 segundo para que cree spec.json y el directorio de run
    time.sleep(1.0)

    # 2. Arrancar el dashboard para este run_id
    proc_dash = subprocess.Popen([
        sys.executable, "-m", "iworkflow.cli", "dashboard",
        run_id
    ])

    print("\nEl dashboard se abrirá automáticamente en tu navegador.")
    print("Mira cómo cambian los colores de los nodos en tiempo real (Gris -> Azul -> Verde)!")
    
    try:
        # Esperar a que el run termine
        proc_run.wait()
        print("\n¡El workflow ha terminado con éxito!")
        print("El servidor del dashboard sigue activo en http://localhost:8000/")
        print("Presiona Ctrl+C para cerrarlo.")
        
        # Mantener el dashboard vivo
        proc_dash.wait()
    except KeyboardInterrupt:
        print("\nCerrando servidores...")
        proc_run.kill()
        proc_dash.kill()

if __name__ == "__main__":
    main()
