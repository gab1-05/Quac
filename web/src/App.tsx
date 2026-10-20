/** Router — the shell wraps every route; unknown paths land on Overview. */
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import Shell from './components/Shell'
import Overview from './pages/Overview'
import Studio from './pages/Studio'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
          <Route path="studio" element={<Studio />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="jobs/:jobId" element={<JobDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
