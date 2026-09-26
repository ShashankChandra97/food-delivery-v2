"""Publish the same dashboard and icon assets used by the local service."""

from pathlib import Path
from shutil import copyfile


root = Path(__file__).resolve().parent
public = root / 'public'
public.mkdir(exist_ok=True)
copyfile(root / 'dashboard.html', public / 'index.html')
copyfile(root / 'lucide.min.js', public / 'lucide.min.js')
copyfile(root / 'LUCIDE-LICENSE', public / 'LUCIDE-LICENSE')
