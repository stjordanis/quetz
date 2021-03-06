import json

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .rest_models import SolveTask
from .solver import MambaSolver
from .utils import timed_lru_cache

router = APIRouter()


@timed_lru_cache(hours=3, maxsize=128)
def get_solver(channels, subdir):
    s = MambaSolver(list(channels), subdir)
    return s


@router.post("/api/mamba/solve", response_class=PlainTextResponse)
def mamba_solve(solve_task: SolveTask):
    channels = tuple(solve_task.channels)
    subdir = solve_task.subdir
    spec = solve_task.spec

    s = get_solver(channels, subdir)
    _, link, _ = s.solve(spec).to_conda()

    data = []
    data_bytes = f"# platform: {subdir}\n\n"
    data_bytes += "@EXPLICIT\n\n"
    for c, pkg, jsn_s in link:
        jsn_content = json.loads(jsn_s)
        url = jsn_content["url"]
        md5 = jsn_content["md5"]
        each_pkg = f"{url}#{md5}"
        data.append(each_pkg)

    data_bytes += "\n".join(data)

    return data_bytes
