import uvicorn
import os

if __name__ == "__main__":
    # Ensure the app directory is in the path
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)