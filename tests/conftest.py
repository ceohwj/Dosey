import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# tests/에서 실행해도 apps/backend/의 app 패키지와 packages/, apps/의 패키지를 import할 수 있도록 합니다.
BACKEND_ROOT = PROJECT_ROOT / "apps" / "backend"
APPS_ROOT = PROJECT_ROOT / "apps"
PACKAGES_ROOT = PROJECT_ROOT / "packages"
for path in (BACKEND_ROOT, APPS_ROOT, PACKAGES_ROOT, PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
