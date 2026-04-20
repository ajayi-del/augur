import asyncio
import structlog
from typing import Dict

logger = structlog.get_logger()

class VenueHealthMonitor:
    """
    Monitors health of all perps venues.
    """
    
    def __init__(self, router):
        self.router = router
        self.health_check_interval = 30  # seconds
        self.consecutive_failures = {}
        self.max_failures = 3
        
    async def monitor_loop(self):
        """Continuous health monitoring"""
        logger.info("venue_health_monitor_started", interval=self.health_check_interval)
        while True:
            for venue in self.router.venues:
                try:
                    healthy = await venue.health_check()
                    
                    if healthy:
                        self.router.venue_health[venue.name] = True
                        self.consecutive_failures[venue.name] = 0
                    else:
                        self._record_failure(venue.name)
                        
                except Exception as e:
                    logger.warning("venue_health_check_failed", 
                                 venue=venue.name, 
                                 error=str(e))
                    self._record_failure(venue.name)
            
            await asyncio.sleep(self.health_check_interval)
                    
    def _record_failure(self, venue_name: str):
        """Track consecutive failures and mark unhealthy if threshold met."""
        self.consecutive_failures[venue_name] = self.consecutive_failures.get(venue_name, 0) + 1
        
        if self.consecutive_failures[venue_name] >= self.max_failures:
            self.router.venue_health[venue_name] = False
            logger.error("venue_marked_unhealthy", 
                        venue=venue_name, 
                        failures=self.consecutive_failures[venue_name])
