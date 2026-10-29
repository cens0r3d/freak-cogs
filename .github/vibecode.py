import sys
import urllib.request


def get_color(pct):
	if pct <= 20:
		return "4cc9f0"
	elif pct <= 40:
		return "4361ee"
	elif pct <= 60:
		return "7209b7"
	elif pct <= 80:
		return "b5179e"
	else:
		return "f72585"


def main():
	try:
		with open(".github/.vibecode") as f:
			pct = int(f.read().strip())
	except FileNotFoundError:
		print("Error: .vibecode not found")
		sys.exit(1)
	except ValueError:
		print("Error: .vibecode must contain an integer 0-100")
		sys.exit(1)

	if not 0 <= pct <= 100:
		print(f"Error: percentage must be 0-100, got {pct}")
		sys.exit(1)

	color = get_color(pct)
	url = f"https://img.shields.io/badge/vibe--coded-{pct}%25-{color}"

	req = urllib.request.Request(url, headers={"User-Agent": "vibecode-badge"})
	with urllib.request.urlopen(req) as resp:
		svg = resp.read()

	with open(".github/vibecode.svg", "wb") as f:
		f.write(svg)

	print(f"Badge written: {pct}% ({color})")


if __name__ == "__main__":
	main()
