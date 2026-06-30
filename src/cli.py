"""
cli.py — Typer-based interface for the Multi-Source Candidate Data Transformer.
"""
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.adapters import ats_json_adapter, csv_adapter, github_adapter, resume_adapter
from src.merge import merge_fragments
from src.pipeline import accumulate_fragments
from src.project import ConfigurableProjector

app = typer.Typer(
    help="Eightfold Multi-Source Candidate Data Transformer",
    add_completion=False,
)
console = Console()

# Setup rich logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, markup=True, rich_tracebacks=True)]
)
logger = logging.getLogger("eightfold.cli")


@app.command()
def process(
    input_dir: Annotated[
        Path, 
        typer.Option(
            "--input-dir", 
            "-i", 
            help="Directory containing source files (CSV, JSON, PDF)."
        )
    ],
    config: Annotated[
        Path, 
        typer.Option(
            "--config", 
            "-c", 
            help="Path to the JSON projection configuration."
        )
    ],
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Run GitHub adapter in offline mode."
        )
    ] = True,
    github_map: Annotated[
        Optional[str],
        typer.Option(
            "--github-map",
            help="Map emails to GitHub usernames (e.g. email1:user1,email2:user2)."
        )
    ] = None,
):
    """
    Execute the batch pipeline against the input directory and project the output.
    """
    console.print(f"[bold blue]Starting Eightfold Data Pipeline[/bold blue]")
    
    try:
        if not input_dir.exists() or not input_dir.is_dir():
            console.print(f"[bold red]Error:[/bold red] Input directory '{input_dir}' not found.")
            raise typer.Exit(1)
            
        if not config.exists():
            console.print(f"[bold red]Error:[/bold red] Config file '{config}' not found.")
            raise typer.Exit(1)
    
        if offline:
            os.environ["OFFLINE_MODE"] = "true"
    
        try:
            with open(config, "r", encoding="utf-8") as f:
                projector_config = json.load(f)
        except Exception as e:
            console.print(f"[bold red]Error reading config:[/bold red] {e}")
            raise typer.Exit(1)

        # 1. Manifest Assembly
        sources = []
    
        # Check for specific mock files or generic globbing
        csv_file = input_dir / "recruiter.csv"
        if csv_file.exists():
            sources.append((csv_adapter, csv_file))
        
        ats_file = input_dir / "ats_candidates.json"
        if ats_file.exists():
            sources.append((ats_json_adapter, ats_file))
        
        for pdf in input_dir.glob("*.pdf"):
            sources.append((resume_adapter, pdf))
        
        adapter_kwargs = {}
        if github_map:
            for mapping in github_map.split(","):
                parts = mapping.split(":")
                if len(parts) == 2:
                    email, username = parts[0].strip(), parts[1].strip()
                    sources.append((github_adapter, username))
                    # Add to adapter_kwargs
                    if "github_adapter" not in adapter_kwargs:
                        adapter_kwargs["github_adapter"] = {}
                    adapter_kwargs["github_adapter"]["candidate_hint"] = {"email": email}
        
        if not sources:
            console.print("[yellow]Warning: No data sources found in input directory.[/yellow]")
            raise typer.Exit(0)

        # 2. Pipeline Execution
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            # Phase 1
            t1 = progress.add_task("[cyan]Ingesting and accumulating fragments...", total=None)
            fragments = accumulate_fragments(sources, adapter_kwargs=adapter_kwargs if 'adapter_kwargs' in locals() else None)
            progress.update(t1, completed=100, description=f"[green]Ingested {len(fragments)} fragments.")

            # Phase 2
            t2 = progress.add_task("[cyan]Merging entities and computing confidence...", total=None)
            profiles, failed_count = merge_fragments(fragments)
            degraded_count = sum(1 for p in profiles if p.overall_confidence <= 0.01)
            progress.update(t2, completed=100, description=f"[green]Merged {len(profiles)} canonical profiles ({failed_count} failures, {degraded_count} degraded).")

            # Phase 3
            t3 = progress.add_task("[cyan]Projecting runtime schema...", total=None)
            projector = ConfigurableProjector(projector_config)
            projected_profiles = []
            proj_failed = 0
            for p in profiles:
                try:
                    projected_profiles.append(projector.project(p))
                except Exception as e:
                    proj_failed += 1
                    logger.error(f"Projection failed for {p.candidate_id}: {e}")
            progress.update(t3, completed=100, description=f"[green]Projected {len(projected_profiles)} custom profiles ({proj_failed} projection failures).")

        # 3. I/O Persistence
        out_dir = Path("data/output")
        out_dir.mkdir(parents=True, exist_ok=True)
    
        canonical_out = out_dir / "default_canonical_output.json"
        projected_out = out_dir / "projected_custom_output.json"
    
        with open(canonical_out, "w", encoding="utf-8") as f:
            json.dump([p.model_dump() for p in profiles], f, indent=2, ensure_ascii=False)
        
        with open(projected_out, "w", encoding="utf-8") as f:
            json.dump(projected_profiles, f, indent=2, ensure_ascii=False)
        
        console.print(f"\n[bold green]Pipeline Complete![/bold green]")
        console.print(f"  ➜ Canonical Output: [dim]{canonical_out}[/dim]")
        console.print(f"  ➜ Projected Output: [dim]{projected_out}[/dim]")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"\n[bold red]Pipeline Failed:[/bold red] {e}")
        logger.exception("Fatal error in process()")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
