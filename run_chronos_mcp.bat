@echo off
set CHRONOS_GROUP_ID=novapay-demo
set CHRONOS_AUTH=permissive
set CHRONOS_REPO_PATH=C:\Users\urbra\OneDrive\Desktop\Projects\New ortho\demo\novapay
set CHRONOS_SQLITE=C:\Users\urbra\OneDrive\Desktop\Projects\New ortho\demo\novapay\.chronos\chronos.db
set CHRONOS_DB=C:\Users\urbra\OneDrive\Desktop\Projects\New ortho\demo\novapay\.chronos\graph.kz

C:\Users\urbra\AppData\Local\Programs\Python\Python312\python.exe -m chronos.server
